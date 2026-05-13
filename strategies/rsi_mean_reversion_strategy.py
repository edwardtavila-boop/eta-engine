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
1. Compute RSI(14) and Bollinger Bands (EMA 20 +/- 2 sigma) on close
   prices.
2. LONG entry: RSI < rsi_long_threshold (20) AND close within BB lower
   band buffer AND volume > avg AND rejection candle (hammer/engulfing)
   AND HTF trend gate agrees (synthetic 1h EMA-50 slope is positive).
3. SHORT entry: RSI > rsi_short_threshold (80) AND close within BB
   upper band buffer AND volume > avg AND rejection candle
   (shooting star / bearish engulfing) AND HTF trend gate agrees
   (synthetic 1h EMA-50 slope is negative).
4. Exit via ATR-based stops and RR targets. Mean-reversion targets are
   smaller than trend-following (RR 1.5-2.0 vs 2.5-3.0) because
   reversals rarely travel full range.
5. Session-restricted: afternoon mean-revert phases only (futures:
   13:30-15:30 ET; crypto: London open 07:00-09:00 UTC).

Designed to be wrapped by ConfluenceScorecardStrategy for supercharged
gating. RSI/BB fires the mechanical trigger; confluence scorecard adds
trend-alignment, VWAP, ATR regime, volume, HTF, session factors.

HTF (higher-timeframe) trend gate (added 2026-05-07)
----------------------------------------------------
Per the 2026-05-07 fleet audit, `rsi_mr_mnq` was the top equity-index
candidate (Sharpe 1.23, expR +0.090, 137 trades) but the audit flagged
the textbook MR failure mode: fading bear capitulations into more bear.

The HTF gate aggregates 12 5m bars into one synthetic 1h close, then
computes an EMA(50) over those 1h closes. The trend signal is:

  slope > 0  if  last_1h_close > ema_50  (HTF uptrend)
  slope < 0  if  last_1h_close < ema_50  (HTF downtrend)

LONG fires only when slope > 0 (don't fade bear capitulations).
SHORT fires only when slope < 0 (don't fade bull breakouts).

Gate is configurable via `require_htf_agreement: bool = True`. When
False, behaviour is identical to the legacy strategy (audit A/B path).

Configurable for asset class
-----------------------------
* MNQ 5m: rsi_period=10, rsi_long=25, rsi_short=75, bb_window=20,
  bb_std=2.0, atr_stop_mult=1.0, rr_target=1.5
* BTC 1h: rsi_period=14, rsi_long=30, rsi_short=70, bb_window=20,
  bb_std=2.0, atr_stop_mult=1.5, rr_target=2.0
* ETH 1h: rsi_period=14, rsi_long=25, rsi_short=75, bb_window=20,
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
    # Legacy thresholds kept for backward compatibility with presets that
    # still set them; not consulted by maybe_enter (the gate uses the
    # *_threshold pair below).  Removing them entirely would break
    # external callers that read the field by name.
    oversold_threshold: float = 30.0
    overbought_threshold: float = 70.0

    # 2026-05-07 HTF audit tuning: tighten from 25/75 -> 20/80 to reduce
    # fire rate but improve quality.  These are the values consulted by
    # maybe_enter; presets override them per asset class.
    rsi_long_threshold: float = 20.0
    rsi_short_threshold: float = 80.0

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

    # HTF (higher-timeframe) trend gate (audit 2026-05-07):
    #   - htf_lookback_5m_bars = 12  -> 12 5m bars per synthetic 1h close
    #   - htf_ema_period       = 50  -> EMA-50 over synthetic 1h closes
    #   - require_htf_agreement       -> when True (default) the gate is
    #     active; when False, behaviour is identical to legacy strategy
    #     (used by grid search to A/B-test the gate).
    htf_lookback_5m_bars: int = 12
    htf_ema_period: int = 50
    require_htf_agreement: bool = True

    # Session filter -- defaults to "off" so 24/7 ticker trading and
    # Globex futures both work without restriction.  Operators may
    # opt in to "afternoon" (08:00-16:00 ET -- currently UTC-naive,
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
        # HTF trend-gate audit counters (added 2026-05-07): how many
        # would-have-fired entries the HTF agreement check blocked.
        self._n_htf_filtered_long: int = 0
        self._n_htf_filtered_short: int = 0

        # Synthetic-1h-EMA state.  We accumulate `htf_lookback_5m_bars`
        # 5m closes into one synthetic 1h close (the last 5m close in
        # the window), then update an EMA over the synthetic series.
        self._htf_5m_count: int = 0
        self._htf_last_5m_close: float | None = None
        self._htf_last_1h_close: float | None = None
        self._htf_ema: float | None = None
        self._htf_ema_seed_buf: list[float] = []
        # EMA smoothing constant -- standard 2/(N+1) form.  Computed once
        # at __init__ rather than re-derived each bar.
        self._htf_alpha: float = 2.0 / (self.cfg.htf_ema_period + 1)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "long_signals": self._n_long_sig,
            "short_signals": self._n_short_sig,
            "vol_rejects": self._n_vol_reject,
            "adx_rejects": self._n_adx_reject,
            "htf_filtered_long": self._n_htf_filtered_long,
            "htf_filtered_short": self._n_htf_filtered_short,
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
        window = list(self._closes)[-self.cfg.bb_window :]
        if len(window) < self.cfg.bb_window:
            return None
        mean = sum(window) / len(window)
        var = sum((c - mean) ** 2 for c in window) / len(window)
        std = var**0.5
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
        std = var**0.5
        if std <= 0.0:
            return 0.0
        return (bar.volume - mean) / std

    def _is_allowed_session(self, bar: BarData) -> bool:
        if self.cfg.session_filter == "afternoon":
            t = bar.timestamp.time()
            return time(8, 0) <= t <= time(16, 0)
        return True

    def _update_htf_state(self, close: float) -> None:
        """Roll the synthetic-1h-EMA forward by one 5m close.

        Every `htf_lookback_5m_bars` 5m bars produces one synthetic 1h
        close (= the last 5m close in the window).  The EMA is seeded
        with a simple mean over the first `htf_ema_period` synthetic 1h
        closes, then updated incrementally.
        """
        self._htf_last_5m_close = close
        self._htf_5m_count += 1
        if self._htf_5m_count < self.cfg.htf_lookback_5m_bars:
            return
        # Window complete -- finalize one synthetic 1h close.
        self._htf_last_1h_close = close
        self._htf_5m_count = 0
        if self._htf_ema is None:
            self._htf_ema_seed_buf.append(close)
            if len(self._htf_ema_seed_buf) >= self.cfg.htf_ema_period:
                self._htf_ema = sum(self._htf_ema_seed_buf) / len(self._htf_ema_seed_buf)
                self._htf_ema_seed_buf = []
        else:
            self._htf_ema = self._htf_alpha * close + (1.0 - self._htf_alpha) * self._htf_ema

    def _htf_slope(self) -> int | None:
        """Return +1 (uptrend), -1 (downtrend), or None (not warm yet).

        Sign convention: positive when last synthetic 1h close > EMA-50,
        negative when below.  Equality returns 0 (treated as "no
        agreement" for both LONG and SHORT by the gate).
        """
        if self._htf_ema is None or self._htf_last_1h_close is None:
            return None
        if self._htf_last_1h_close > self._htf_ema:
            return 1
        if self._htf_last_1h_close < self._htf_ema:
            return -1
        return 0

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
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
        self._update_htf_state(bar.close)

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
        if self.cfg.allow_long and rsi <= self.cfg.rsi_long_threshold:
            if bar.close <= bb_lower + buffer:
                side = "BUY"
                self._n_long_sig += 1
        elif self.cfg.allow_short and rsi >= self.cfg.rsi_short_threshold and bar.close >= bb_upper - buffer:
            side = "SELL"
            self._n_short_sig += 1

        if side is None:
            return None

        # HTF trend gate: only fade RSI extremes when the higher
        # timeframe agrees with the mean-reversion direction.  Audit
        # 2026-05-07 -- prevents fading bear capitulations in a
        # downtrend (and bull breakouts in an uptrend).
        if self.cfg.require_htf_agreement:
            slope = self._htf_slope()
            # Until the HTF EMA seeds (`htf_ema_period` synthetic 1h
            # closes), we have no trend signal -- block to stay
            # conservative.  This is a one-time warmup cost; live runs
            # accumulate the EMA across restarts via persisted state
            # (or simply burn the first ~50h of bars).
            if slope is None:
                if side == "BUY":
                    self._n_htf_filtered_long += 1
                else:
                    self._n_htf_filtered_short += 1
                return None
            if side == "BUY" and slope <= 0:
                self._n_htf_filtered_long += 1
                return None
            if side == "SELL" and slope >= 0:
                self._n_htf_filtered_short += 1
                return None

        if not self._is_rejection(bar, side):
            return None

        if self.cfg.enable_adx_filter and len(self._highs) >= self.cfg.adx_period * 2 + 1:
            from eta_engine.strategies.technical_edges import compute_adx

            adx_result = compute_adx(
                list(self._highs),
                list(self._lows),
                list(self._closes),
                self.cfg.adx_period,
            )
            if adx_result is not None and adx_result.adx > self.cfg.adx_max:
                self._n_adx_reject += 1
                return None

        vz = self._volume_z_score(bar)
        if vz < self.cfg.min_volume_z:
            self._n_vol_reject += 1
            return None

        atr_window = hist[-self.cfg.atr_period :] if hist else []
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
            entry_bar=bar,
            side=side,
            qty=qty,
            entry_price=entry,
            stop=stop,
            target=target,
            risk_usd=risk_usd,
            confluence=8.0,
            leverage=1.0,
            regime=f"rsi_mr_{side.lower()}_rsi{rsi:.0f}",
        )


def mnq_rsi_mr_preset() -> RSIMeanReversionConfig:
    """Paper-soak v2 tuning (2026-05-06): atr_stop_mult 1.0->1.5 (tight
    stops were getting hit on noise before the mean-reversion played out
    on ~50 trades at near-breakeven), rr_target 1.5->2.0 (need bigger
    wins to justify the tight-signal premium).

    HTF audit (2026-05-07): tightened RSI thresholds 25/75 -> 20/80 and
    activated the HTF trend gate to avoid fading bear capitulations."""
    return RSIMeanReversionConfig(
        rsi_period=10,
        oversold_threshold=25.0,
        overbought_threshold=75.0,
        rsi_long_threshold=20.0,
        rsi_short_threshold=80.0,
        bb_window=20,
        bb_std_mult=2.0,
        volume_z_lookback=20,
        min_volume_z=0.3,
        require_rejection=True,
        atr_period=14,
        atr_stop_mult=1.5,
        rr_target=2.0,
        risk_per_trade_pct=0.005,
        min_bars_between_trades=12,
        max_trades_per_day=3,
        warmup_bars=50,
        htf_lookback_5m_bars=12,
        htf_ema_period=50,
        require_htf_agreement=True,
    )


def nq_rsi_mr_preset() -> RSIMeanReversionConfig:
    return RSIMeanReversionConfig(
        rsi_period=10,
        oversold_threshold=25.0,
        overbought_threshold=75.0,
        rsi_long_threshold=20.0,
        rsi_short_threshold=80.0,
        bb_window=20,
        bb_std_mult=2.0,
        volume_z_lookback=20,
        min_volume_z=0.3,
        require_rejection=True,
        atr_period=14,
        atr_stop_mult=1.0,
        rr_target=1.5,
        risk_per_trade_pct=0.005,
        min_bars_between_trades=12,
        max_trades_per_day=3,
        warmup_bars=50,
        htf_lookback_5m_bars=12,
        htf_ema_period=50,
        require_htf_agreement=True,
    )


def btc_rsi_mr_preset() -> RSIMeanReversionConfig:
    return RSIMeanReversionConfig(
        rsi_period=14,
        oversold_threshold=30.0,
        overbought_threshold=70.0,
        rsi_long_threshold=20.0,
        rsi_short_threshold=80.0,
        bb_window=20,
        bb_std_mult=2.0,
        volume_z_lookback=24,
        min_volume_z=0.2,
        require_rejection=True,
        atr_period=14,
        atr_stop_mult=1.5,
        rr_target=2.0,
        risk_per_trade_pct=0.005,
        min_bars_between_trades=12,
        max_trades_per_day=2,
        warmup_bars=72,
        htf_lookback_5m_bars=12,
        htf_ema_period=50,
        require_htf_agreement=True,
    )


def eth_rsi_mr_preset() -> RSIMeanReversionConfig:
    return RSIMeanReversionConfig(
        rsi_period=14,
        oversold_threshold=25.0,
        overbought_threshold=75.0,
        rsi_long_threshold=20.0,
        rsi_short_threshold=80.0,
        bb_window=20,
        bb_std_mult=2.5,
        volume_z_lookback=24,
        min_volume_z=0.2,
        require_rejection=True,
        atr_period=14,
        atr_stop_mult=1.8,
        rr_target=2.0,
        risk_per_trade_pct=0.005,
        min_bars_between_trades=12,
        max_trades_per_day=2,
        warmup_bars=72,
        htf_lookback_5m_bars=12,
        htf_ema_period=50,
        require_htf_agreement=True,
    )
