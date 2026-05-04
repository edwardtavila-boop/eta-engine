"""
EVOLUTIONARY TRADING ALGO  //  strategies.gap_fill_strategy
============================================================
Overnight gap fill strategy — trade the highest-probability single
session edge in futures markets.

Empirical edge: ~80% of overnight gaps fill within the first 2 hours
of RTH. This is the same edge institutional gap-trading desks exploit
daily — the opening auction resets the equilibrium, and price tends
to migrate back toward the prior session's close.

Mechanic
--------
1. Detect overnight gap: |today_open - yesterday_close| > gap_threshold * ATR.
   - Gap up: today's open > yesterday's close by significant margin
   - Gap down: today's open < yesterday's close by significant margin
2. After gap detection, monitor the first N bars of the session for
   a reversal candle indicating gap fill has begun.
3. LONG entry (gap down): gap detected AND price shows rejection at
   session low (hammer / bullish reversal) AND volume confirms.
   Target: prior close (gap fill). Stop: below session low.
4. SHORT entry (gap up): gap detected AND price shows rejection at
   session high (shooting star / bearish reversal) AND volume confirms.
   Target: prior close. Stop: above session high.
5. Session-restricted: entries only in first 2 hours of RTH
   (09:30-11:30 ET for futures) or first 4 bars of crypto session.

Designed to be wrapped by ConfluenceScorecardStrategy.

Configurable for asset class
-----------------------------
* MNQ 5m: gap_threshold=0.5 ATR, fill_window=24 bars (2h RTH),
  atr_stop_mult=1.0, rr_target=2.0
* BTC 1h: gap_threshold=1.0 ATR, fill_window=6 bars (6h crypto),
  atr_stop_mult=1.5, rr_target=2.0
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class GapFillConfig:
    gap_threshold_atr_mult: float = 0.5
    fill_max_bars: int = 24
    gap_min_atr_mult: float = 0.3

    volume_z_lookback: int = 20
    min_volume_z: float = 0.3
    require_rejection: bool = True

    atr_period: int = 14
    atr_stop_mult: float = 1.0
    rr_target: float = 2.0
    risk_per_trade_pct: float = 0.005

    min_bars_between_trades: int = 24
    max_trades_per_day: int = 1
    warmup_bars: int = 50
    min_session_gap_hours: float = 4.0
    enter_immediate: bool = False

    allow_long: bool = True
    allow_short: bool = True


class GapFillStrategy:

    def __init__(self, config: GapFillConfig | None = None) -> None:
        self.cfg = config or GapFillConfig()
        self._volume_window: deque[float] = deque(maxlen=self.cfg.volume_z_lookback)
        self._yesterday_close: float | None = None
        self._today_open: float | None = None
        self._session_bars: int = 0
        self._gap_side: str | None = None
        self._gap_size: float = 0.0
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None
        self._n_gaps_detected: int = 0
        self._n_long_sig: int = 0
        self._n_short_sig: int = 0
        self._n_vol_reject: int = 0
        self._n_fired: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "gaps_detected": self._n_gaps_detected,
            "long_signals": self._n_long_sig,
            "short_signals": self._n_short_sig,
            "vol_rejects": self._n_vol_reject,
            "entries_fired": self._n_fired,
        }

    def _is_rejection(self, bar: BarData, side: str) -> bool:
        if not self.cfg.require_rejection:
            return True
        total_range = max(bar.high - bar.low, 1e-9)
        if side == "BUY":
            lower_wick = min(bar.open, bar.close) - bar.low
            upper_wick = bar.high - max(bar.open, bar.close)
            if lower_wick > upper_wick and lower_wick / total_range > 0.30:
                return True
            if bar.close > bar.open and bar.close > bar.open + (bar.high - bar.low) * 0.15:
                return True
        else:
            upper_wick = bar.high - max(bar.open, bar.close)
            lower_wick = min(bar.open, bar.close) - bar.low
            if upper_wick > lower_wick and upper_wick / total_range > 0.30:
                return True
            if bar.close < bar.open and bar.close < bar.open - (bar.high - bar.low) * 0.15:
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

    def maybe_enter(
        self, bar: BarData, hist: list[BarData],
        equity: float, config: BacktestConfig,
    ) -> _Open | None:
        bar_date = bar.timestamp.date()

        if self._last_day is None or bar_date != self._last_day:
            if self._last_day is not None and len(hist) >= 1:
                self._yesterday_close = hist[-1].close
            self._last_day = bar_date
            self._trades_today = 0
            self._session_bars = 0
            self._gap_side = None
            self._gap_size = 0.0
            self._today_open = bar.open

            if self._yesterday_close is not None and len(hist) >= 2:
                time_gap_hours = (bar.timestamp - hist[-1].timestamp).total_seconds() / 3600.0
                if time_gap_hours >= self.cfg.min_session_gap_hours:
                    atr_window = hist[-self.cfg.atr_period:] if len(hist) >= self.cfg.atr_period else hist
                    if len(atr_window) >= 2:
                        atr = sum(b.high - b.low for b in atr_window) / len(atr_window)
                        gap = abs(self._today_open - self._yesterday_close)
                        if gap > self.cfg.gap_min_atr_mult * atr:
                            self._n_gaps_detected += 1
                            if self._today_open > self._yesterday_close:
                                self._gap_side = "SELL"
                            else:
                                self._gap_side = "BUY"
                            self._gap_size = gap

        self._bars_seen += 1
        self._session_bars += 1
        self._volume_window.append(bar.volume)

        if self._bars_seen < self.cfg.warmup_bars:
            return None
        if self._gap_side is None:
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx) < self.cfg.min_bars_between_trades
        ):
            return None

        if self.cfg.enter_immediate and self._session_bars == 1:
            side = self._gap_side
            if side == "BUY":
                if not self.cfg.allow_long:
                    return None
            else:
                if not self.cfg.allow_short:
                    return None
            # Skip reversal/wait logic — enter immediately
        elif self._session_bars > self.cfg.fill_max_bars:
            return None
        else:
            side = self._gap_side
            if side == "BUY":
                if not self.cfg.allow_long:
                    return None
                if len(hist) >= 2 and bar.close <= hist[-1].close:
                    return None
                self._n_long_sig += 1
            else:
                if not self.cfg.allow_short:
                    return None
                if len(hist) >= 2 and bar.close >= hist[-1].close:
                    return None
                self._n_short_sig += 1

            if not self._is_rejection(bar, side):
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
        if qty <= 0.0:
            return None

        entry = bar.close
        # Float-truthy fix: yesterday's close of 0.0 (synthetic test bar)
        # would silently fall back to entry, producing a zero-distance
        # target.  Use explicit None check.
        prior_close = self._yesterday_close if self._yesterday_close is not None else entry
        if side == "BUY":
            stop = min(entry - stop_dist, bar.low - atr * 0.1)
            stop_dist_actual = entry - stop
            target = prior_close if prior_close > entry else entry + self.cfg.rr_target * stop_dist_actual
        else:
            stop = max(entry + stop_dist, bar.high + atr * 0.1)
            stop_dist_actual = stop - entry
            target = prior_close if prior_close < entry else entry - self.cfg.rr_target * stop_dist_actual

        from eta_engine.backtest.engine import _Open

        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        self._n_fired += 1
        return _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=9.0, leverage=1.0,
            regime=f"gap_fill_{side.lower()}_gap{self._gap_size:.1f}",
        )


def mnq_gap_fill_preset() -> GapFillConfig:
    return GapFillConfig(
        gap_threshold_atr_mult=0.2, fill_max_bars=48, gap_min_atr_mult=0.15,
        min_session_gap_hours=4.0,
        volume_z_lookback=20, min_volume_z=0.2, require_rejection=False,
        atr_period=14, atr_stop_mult=1.0, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=50,
    )


def nq_gap_fill_preset() -> GapFillConfig:
    return GapFillConfig(
        gap_threshold_atr_mult=0.2, fill_max_bars=48, gap_min_atr_mult=0.15,
        min_session_gap_hours=4.0,
        volume_z_lookback=20, min_volume_z=0.2, require_rejection=False,
        atr_period=14, atr_stop_mult=1.0, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=50,
    )


def btc_gap_fill_preset() -> GapFillConfig:
    return GapFillConfig(
        gap_threshold_atr_mult=1.0, fill_max_bars=12, gap_min_atr_mult=0.5,
        min_session_gap_hours=12.0,
        volume_z_lookback=24, min_volume_z=0.2, require_rejection=True,
        atr_period=14, atr_stop_mult=1.5, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=1, warmup_bars=72,
    )


def eth_gap_fill_preset() -> GapFillConfig:
    return GapFillConfig(
        gap_threshold_atr_mult=1.0, fill_max_bars=12, gap_min_atr_mult=0.5,
        min_session_gap_hours=12.0,
        volume_z_lookback=24, min_volume_z=0.2, require_rejection=True,
        atr_period=14, atr_stop_mult=1.8, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=1, warmup_bars=72,
    )
