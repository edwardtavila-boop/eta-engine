"""
EVOLUTIONARY TRADING ALGO  //  strategies.rsi_mean_reversion_strategy
=====================================================================
Counter-trend mean-reversion on RSI extremes + Bollinger Band touches.

This fills the single biggest gap in the strategy portfolio: every
diamond-tier strategy is trend-following or breakout. Mean-reversion
thrives in the regime where existing strategies fail (range-bound,
choppy, afternoon sessions).

Mechanic
--------
1. Compute RSI(14) and Bollinger Bands (EMA 20 ± 2σ) on close prices.
2. LONG entry: RSI < oversold_threshold (30) AND close within BB lower
   band buffer AND volume > avg AND rejection candle (hammer/engulfing).
3. SHORT entry: RSI > overbought_threshold (70) AND close within BB
   upper band buffer AND volume > avg AND rejection candle
   (shooting star / bearish engulfing).
4. Exit via ATR-based stops and RR targets. Mean-reversion targets are
   smaller than trend-following (RR 1.5-2.0 vs 2.5-3.0) because
   reversals rarely travel full range.
5. Session-restricted: afternoon mean-revert phases only (futures:
   13:30-15:30 ET; crypto: London open 07:00-09:00 UTC).

Designed to be wrapped by ConfluenceScorecardStrategy for supercharged
gating. RSI/BB fires the mechanical trigger; confluence scorecard adds
trend-alignment, VWAP, ATR regime, volume, HTF, session factors.

Configurable for asset class
-----------------------------
* MNQ 5m: rsi_period=10, oversold=25, overbought=75, bb_window=20,
  bb_std=2.0, atr_stop_mult=1.0, rr_target=1.5
* BTC 1h: rsi_period=14, oversold=30, overbought=70, bb_window=20,
  bb_std=2.0, atr_stop_mult=1.5, rr_target=2.0
* ETH 1h: rsi_period=14, oversold=25, overbought=75, bb_window=20,
  bb_std=2.5, atr_stop_mult=1.8, rr_target=2.0 (wider bands for ETH vol)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class RSIMeanReversionConfig:
    rsi_period: int = 14
    oversold_threshold: float = 30.0
    overbought_threshold: float = 70.0

    bb_window: int = 20
    bb_std_mult: float = 2.0

    adx_period: int = 14
    adx_max: float = 25.0
    # Default ON.  Mean-reversion in trending regimes is the textbook way
    # to bleed; the ADX filter (already implemented at line ~244) protects
    # the strategy from firing into trend days.  Flipping the default is
    # the single highest-leverage one-line change in the MR stack.
    enable_adx_filter: bool = True

    volume_z_lookback: int = 20
    min_volume_z: float = 0.3
    require_rejection: bool = True

    atr_period: int = 14
    atr_stop_mult: float = 1.5
    rr_target: float = 1.5
    risk_per_trade_pct: float = 0.005

    min_bars_between_trades: int = 12
    max_trades_per_day: int = 2
    warmup_bars: int = 50

    allow_long: bool = True
    allow_short: bool = True

    # Session filter — defaults to "off" so 24/7 ticker trading and
    # Globex futures both work without restriction.  Operators may
    # opt in to "afternoon" (08:00-16:00 ET — currently UTC-naive,
    # see same caveat as VWAP MR).  Other modes can be added.
    session_filter: str = "off"


class RSIMeanReversionStrategy:

    def __init__(self, config: RSIMeanReversionConfig | None = None) -> None:
        self.cfg = config or RSIMeanReversionConfig()
        self._closes: deque[float] = deque(maxlen=max(self.cfg.bb_window, self.cfg.rsi_period) + 5)
        self._highs: deque[float] = deque(maxlen=self.cfg.bb_window + 5)
        self._lows: deque[float] = deque(maxlen=self.cfg.bb_window + 5)
        self._volume_window: deque[float] = deque(maxlen=self.cfg.volume_z_lookback)
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None
        self._n_long_sig: int = 0
        self._n_short_sig: int = 0
        self._n_vol_reject: int = 0
        self._n_adx_reject: int = 0
        self._n_fired: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "long_signals": self._n_long_sig,
            "short_signals": self._n_short_sig,
            "vol_rejects": self._n_vol_reject,
            "adx_rejects": self._n_adx_reject,
            "entries_fired": self._n_fired,
        }

    def _compute_rsi(self) -> float | None:
        if len(self._closes) < self.cfg.rsi_period + 1:
            return None
        closes = list(self._closes)
        changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(c, 0.0) for c in changes]
        losses = [max(-c, 0.0) for c in changes]
        period = self.cfg.rsi_period
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        if avg_loss == 0:
            return 100.0
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def _compute_bb(self) -> tuple[float, float, float] | None:
        window = list(self._closes)[-self.cfg.bb_window:]
        if len(window) < self.cfg.bb_window:
            return None
        mean = sum(window) / len(window)
        var = sum((c - mean) ** 2 for c in window) / len(window)
        std = var ** 0.5
        mult = self.cfg.bb_std_mult
        return (mean + mult * std, mean, mean - mult * std)

    def _is_rejection(self, bar: BarData, side: str) -> bool:
        if not self.cfg.require_rejection:
            return True
        body = abs(bar.close - bar.open)
        total_range = max(bar.high - bar.low, 1e-9)
        body_ratio = body / total_range
        if side == "BUY":
            lower_wick = min(bar.open, bar.close) - bar.low
            upper_wick = bar.high - max(bar.open, bar.close)
            if lower_wick > upper_wick and body_ratio > 0.20:
                return True
            if bar.close > bar.open and body_ratio > 0.50:
                return True
        else:
            upper_wick = bar.high - max(bar.open, bar.close)
            lower_wick = min(bar.open, bar.close) - bar.low
            if upper_wick > lower_wick and body_ratio > 0.20:
                return True
            if bar.close < bar.open and body_ratio > 0.50:
                return True
        return False

    def _volume_z_score(self, bar: BarData) -> float:
        if len(self._volume_window) < self.cfg.volume_z_lookback:
            return 0.0
        vols = list(self._volume_window)
        mean = sum(vols) / len(vols)
        var = sum((v - mean) ** 2 for v in vols) / len(vols)
        std = var ** 0.5
        if std <= 0.0:
            return 0.0
        return (bar.volume - mean) / std

    def _is_allowed_session(self, bar: BarData) -> bool:
        if self.cfg.session_filter == "afternoon":
            t = bar.timestamp.time()
            return time(8, 0) <= t <= time(16, 0)
        return True

    def maybe_enter(
        self, bar: BarData, hist: list[BarData],
        equity: float, config: BacktestConfig,
    ) -> _Open | None:
        bar_date = bar.timestamp.date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0
        self._bars_seen += 1

        self._closes.append(bar.close)
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._volume_window.append(bar.volume)

        if self._bars_seen < self.cfg.warmup_bars:
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx) < self.cfg.min_bars_between_trades
        ):
            return None

        rsi = self._compute_rsi()
        bb = self._compute_bb()
        if rsi is None or bb is None:
            return None

        bb_upper, bb_mid, bb_lower = bb
        bb_range = max(bb_upper - bb_lower, 1e-9)
        buffer = bb_range * 0.10

        side: str | None = None
        if self.cfg.allow_long and rsi <= self.cfg.oversold_threshold:
            if bar.close <= bb_lower + buffer:
                side = "BUY"
                self._n_long_sig += 1
        elif (
            self.cfg.allow_short
            and rsi >= self.cfg.overbought_threshold
            and bar.close >= bb_upper - buffer
        ):
            side = "SELL"
            self._n_short_sig += 1

        if side is None:
            return None

        if not self._is_rejection(bar, side):
            return None

        if (
            self.cfg.enable_adx_filter
            and len(self._highs) >= self.cfg.adx_period * 2 + 1
        ):
            from eta_engine.strategies.technical_edges import compute_adx
            adx_result = compute_adx(
                list(self._highs), list(self._lows), list(self._closes),
                self.cfg.adx_period,
            )
            if adx_result is not None and adx_result.adx > self.cfg.adx_max:
                self._n_adx_reject += 1
                return None

        vz = self._volume_z_score(bar)
        if vz < self.cfg.min_volume_z:
            self._n_vol_reject += 1
            return None

        atr_window = hist[-self.cfg.atr_period:] if hist else []
        if len(atr_window) < 2:
            return None
        atr = sum(b.high - b.low for b in atr_window) / len(atr_window)
        if atr <= 0.0:
            return None
        stop_dist = self.cfg.atr_stop_mult * atr
        if stop_dist <= 0.0:
            return None
        risk_usd = equity * self.cfg.risk_per_trade_pct
        qty = risk_usd / stop_dist
        # Bug fix 2026-05-05: when ATR is unusually small (low-vol bar),
        # qty = risk/stop_dist can blow past the 50x equity notional cap
        # the validator enforces.  Cap qty by notional to keep us inside
        # the cap with a 5% margin.  Was firing 1 notional_exceeds_cap
        # rejection in rsi_mr_mnq elite-gate.
        # point_value isn't in scope here; use a conservative default
        # (MNQ-shaped futures).  Adjust if extending to other instruments.
        point_value_default = 2.0
        if entry := bar.close:
            max_qty_by_notional = (47.5 * equity) / (entry * point_value_default)
            qty = min(qty, max_qty_by_notional)
        if qty <= 0.0:
            return None

        entry = bar.close
        if side == "BUY":
            structure_stop = bar.low - atr * 0.2
            atr_stop = entry - stop_dist
            stop = min(structure_stop, atr_stop)
            stop_dist_actual = entry - stop
            target = entry + self.cfg.rr_target * stop_dist_actual
        else:
            structure_stop = bar.high + atr * 0.2
            atr_stop = entry + stop_dist
            stop = max(structure_stop, atr_stop)
            stop_dist_actual = stop - entry
            target = entry - self.cfg.rr_target * stop_dist_actual

        from eta_engine.backtest.engine import _Open

        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        self._n_fired += 1
        return _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=8.0, leverage=1.0,
            regime=f"rsi_mr_{side.lower()}_rsi{rsi:.0f}",
        )


def mnq_rsi_mr_preset() -> RSIMeanReversionConfig:
    return RSIMeanReversionConfig(
        rsi_period=10, oversold_threshold=25.0, overbought_threshold=75.0,
        bb_window=20, bb_std_mult=2.0,
        volume_z_lookback=20, min_volume_z=0.3, require_rejection=True,
        atr_period=14, atr_stop_mult=1.0, rr_target=1.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=3, warmup_bars=50,
    )


def nq_rsi_mr_preset() -> RSIMeanReversionConfig:
    return RSIMeanReversionConfig(
        rsi_period=10, oversold_threshold=25.0, overbought_threshold=75.0,
        bb_window=20, bb_std_mult=2.0,
        volume_z_lookback=20, min_volume_z=0.3, require_rejection=True,
        atr_period=14, atr_stop_mult=1.0, rr_target=1.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=3, warmup_bars=50,
    )


def btc_rsi_mr_preset() -> RSIMeanReversionConfig:
    return RSIMeanReversionConfig(
        rsi_period=14, oversold_threshold=30.0, overbought_threshold=70.0,
        bb_window=20, bb_std_mult=2.0,
        volume_z_lookback=24, min_volume_z=0.2, require_rejection=True,
        atr_period=14, atr_stop_mult=1.5, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
    )


def eth_rsi_mr_preset() -> RSIMeanReversionConfig:
    return RSIMeanReversionConfig(
        rsi_period=14, oversold_threshold=25.0, overbought_threshold=75.0,
        bb_window=20, bb_std_mult=2.5,
        volume_z_lookback=24, min_volume_z=0.2, require_rejection=True,
        atr_period=14, atr_stop_mult=1.8, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
    )
