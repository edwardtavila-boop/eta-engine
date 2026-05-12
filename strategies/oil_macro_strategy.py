"""Oil Macro Fade Strategy — CL-specific edge for turbulent/headline-driven markets.

Oil in 2026: tariffs, OPEC, Middle East create gap-driven volatility.
Normal momentum fails because trend changes on every headline.
This strategy fades extreme moves — enter on the mean-reversion after
a 2+ ATR spike, assuming headlines fade and price returns to range.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


# ── Session windows for cl_macro entries (2026-05-12 refinement) ──
#
# Oil spikes don't happen uniformly across the day.  The dominant
# catalysts are:
#   - EIA crude inventory release: Wednesday 10:30 ET (14:30 UTC)
#   - Asia open: 18:00-21:00 ET (23:00-02:00 UTC) — overnight headlines
#   - NY morning: 08:00-11:00 ET (12:00-15:00 UTC) — Mideast headlines
# Pre-market thin-liquidity hours (21:00-02:00 ET) produce wicks that
# trigger the spike detector but fill at garbage prices.  The session
# gate rejects entries outside the catalyst windows.
#
# Hours are UTC.  Each tuple is (start, end_exclusive) in hours.
DEFAULT_ALLOWED_HOURS_UTC: tuple[tuple[int, int], ...] = (
    (12, 16),   # NY morning (08:00-12:00 ET)
    (14, 16),   # EIA window (Wednesdays only — extra-tight band)
    (23, 24),   # Asia open part 1
    (0, 3),     # Asia open part 2 (wraps midnight UTC)
)


def _hour_in_windows(hour_utc: int,
                       windows: tuple[tuple[int, int], ...]) -> bool:
    for lo, hi in windows:
        if lo <= hi:
            if lo <= hour_utc < hi:
                return True
        else:
            # Window wraps midnight (lo > hi); shouldn't happen in our
            # config but handle defensively.
            if hour_utc >= lo or hour_utc < hi:
                return True
    return False


@dataclass(frozen=True)
class OilMacroConfig:
    spike_atr_mult: float = 2.0     # Bar range must be 2x ATR to trigger
    volume_z_lookback: int = 24
    min_volume_z: float = 0.3       # Spike must have volume confirmation
    fade_atr_mult: float = 0.5      # Fade the spike by this much ATR
    atr_period: int = 14
    atr_stop_mult: float = 3.0      # Wide stop for continued volatility
    rr_target: float = 3.5          # High RR for macro fade
    risk_per_trade_pct: float = 0.005
    min_bars_between_trades: int = 12
    max_trades_per_day: int = 3
    warmup_bars: int = 72
    # 2026-05-12 refinements
    min_atr_usd: float = 0.20       # Reject signals when ATR<this (dead-tape gate)
    allowed_hours_utc: tuple[tuple[int, int], ...] = field(
        default_factory=lambda: DEFAULT_ALLOWED_HOURS_UTC)
    enforce_session_gate: bool = True
    panic_day_count_window: int = 30  # Track 2x-ATR bar count over N days
    panic_day_min_per_30d: int = 4    # Falsification trigger threshold


class OilMacroStrategy:
    """Fade extreme oil moves — enter against the spike, wide stops."""

    def __init__(self, cfg: OilMacroConfig | None = None) -> None:
        self.cfg = cfg or OilMacroConfig()
        self._tr_window: deque[float] = deque(maxlen=self.cfg.atr_period)
        self._volume_window: deque[float] = deque(maxlen=self.cfg.volume_z_lookback)
        self._close_window: deque[float] = deque(maxlen=48)
        self._bars_since_last_trade: int = 999
        self._trades_today: int = 0
        self._bars_seen: int = 0
        # Panic-day counter (2026-05-12 — moves operator's falsification
        # criterion from prose into code).  Tracks dates on which at
        # least one bar's range exceeded spike_atr_mult * ATR.
        self._panic_dates: deque[str] = deque(
            maxlen=self.cfg.panic_day_count_window)
        self._last_panic_date: str | None = None

    def maybe_enter(self, bar: BarData, hist: list[BarData], equity: float, config: BacktestConfig) -> _Open | None:
        self._bars_seen += 1
        self._update(bar, hist)
        if self._bars_seen < self.cfg.warmup_bars:
            return None
        self._bars_since_last_trade += 1
        if self._bars_since_last_trade < self.cfg.min_bars_between_trades:
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None

        atr = self._atr()
        bar_range_val = bar.high - bar.low

        # ATR floor (2026-05-12): reject signals during dead-tape
        # windows where ATR is unrealistically small.  Without this
        # gate the warmup-period ATR=1.0 fallback meant any 2-tick
        # bar fired a "spike" — false signals at quiet hours.
        if atr < self.cfg.min_atr_usd:
            return None

        # Must be a spike (2x ATR range)
        if bar_range_val < atr * self.cfg.spike_atr_mult:
            return None

        # Track panic days (one date can produce multiple panic bars
        # but we count the DATE once for the operator's falsification
        # criterion).
        bar_dt = self._bar_datetime(bar)
        if bar_dt is not None:
            date_key = bar_dt.strftime("%Y-%m-%d")
            if date_key != self._last_panic_date:
                self._panic_dates.append(date_key)
                self._last_panic_date = date_key

        # Session gate (2026-05-12): oil spikes outside the catalyst
        # windows (NY morning, EIA Wed, Asia open) tend to be illiquid
        # wicks that fill at garbage prices.  Default-on; operator
        # disables via enforce_session_gate=False if backtesting full
        # 24h coverage.
        if (
            self.cfg.enforce_session_gate
            and bar_dt is not None
            and not _hour_in_windows(bar_dt.hour, self.cfg.allowed_hours_utc)
        ):
            return None

        # Volume confirmation
        if not self._volume_ok(bar):
            return None

        # Fade direction: if bar is bearish (close < open), fade it = BUY
        # If bar is bullish (close > open), fade it = SELL
        is_bearish_spike = bar.close < bar.open
        is_bullish_spike = bar.close > bar.open

        # Fade: buy the bear spike, sell the bull spike
        if is_bearish_spike:
            side = "BUY"
            entry = bar.close
            # Fade entry: enter near the low (where the panic was)
            stop_dist = atr * self.cfg.atr_stop_mult
            stop = entry - stop_dist
            target = entry + stop_dist * self.cfg.rr_target
        elif is_bullish_spike:
            side = "SELL"
            entry = bar.close
            stop_dist = atr * self.cfg.atr_stop_mult
            stop = entry + stop_dist
            target = entry - stop_dist * self.cfg.rr_target
        else:
            return None

        risk_usd = equity * self.cfg.risk_per_trade_pct
        qty = risk_usd / max(stop_dist, 1e-9)
        if qty <= 0:
            return None

        self._bars_since_last_trade = 0
        self._trades_today += 1

        from eta_engine.backtest.engine import _Open
        return _Open(
            entry_bar=bar, side=side, qty=qty,
            entry_price=entry, stop=stop, target=target,
            risk_usd=risk_usd, confluence=5.0, leverage=1.0,
            regime=f"oil_fade_{side.lower()}",
        )

    def _update(self, bar: BarData, hist: list[BarData]) -> None:
        self._volume_window.append(bar.volume)
        self._close_window.append(bar.close)
        prev = hist[-1].close if hist else bar.open
        tr = max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev))
        self._tr_window.append(tr)

    def _atr(self) -> float:
        if len(self._tr_window) < self.cfg.atr_period:
            return 1.0
        return sum(self._tr_window) / len(self._tr_window)

    def _bar_datetime(self, bar: BarData) -> datetime | None:
        """Best-effort timestamp extraction.  BarData carries `ts` as
        either ISO string or epoch float across the codebase; handle
        both."""
        ts = getattr(bar, "ts", None) or getattr(bar, "timestamp_utc", None)
        if ts is None:
            return None
        try:
            if isinstance(ts, str):
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if isinstance(ts, (int, float)):
                return datetime.fromtimestamp(float(ts), UTC)
        except (TypeError, ValueError):
            return None
        return None

    def panic_days_in_window(self) -> int:
        """Number of distinct dates with at least one panic bar over
        the configured rolling window.  Operator's falsification
        criterion fires when this drops below panic_day_min_per_30d."""
        return len(set(self._panic_dates))

    def falsification_triggered(self) -> bool:
        """True when panic days < operator's pre-committed floor.
        The dashboard / watchdog reads this on the live instance."""
        return self.panic_days_in_window() < self.cfg.panic_day_min_per_30d

    def _volume_ok(self, bar: BarData) -> bool:
        vols = list(self._volume_window)
        if len(vols) < self.cfg.volume_z_lookback:
            return True  # Not enough data — allow
        mean_v = sum(vols) / len(vols)
        std_v = (sum((v - mean_v) ** 2 for v in vols) / len(vols)) ** 0.5
        if std_v < 1e-9:
            return True
        return (bar.volume - mean_v) / std_v >= self.cfg.min_volume_z


def cl_macro_fade_preset() -> OilMacroConfig:
    """Crude oil macro fade — enter against 2x ATR spikes, wide 3x stops, 3.5 RR."""
    return OilMacroConfig(
        spike_atr_mult=2.0, volume_z_lookback=24, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=3.0, rr_target=3.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=3, warmup_bars=72,
    )
