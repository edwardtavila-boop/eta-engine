"""
EVOLUTIONARY TRADING ALGO  //  strategies.mbt_overnight_gap_strategy
=====================================================================
MBT (CME Bitcoin Micro Future) — Asia overnight gap fade at NY RTH open.

Concept
-------
CME bitcoin futures trade nearly 24h, but liquidity drops off
sharply during Asia hours. Whatever flow does run in Asia tends to
push price into thin books — funds rolling positions, prop desks
hedging, single-broker liquidations. By the time the NY RTH open
re-engages the deepest order books (~08:30 CT) the Asia move
frequently reverses as US arbitrage capital re-anchors price to
spot BTC and the prior US close.

Mechanic
--------
1. Track the most-recent RTH close as the "anchor". When a new
   RTH session begins, compute the overnight gap = (RTH-open price)
   minus prior-RTH-close.
2. If the gap exceeds ``min_gap_atr_mult`` x ATR but is below
   ``max_gap_atr_mult`` (over-large gaps are news-driven and trend,
   not fade):
   - Gap up -> fade SHORT at NY open
   - Gap down -> fade LONG at NY open
3. Trade window: only the first ``entry_window_bars`` bars after
   the RTH open are eligible. Beyond that window the gap edge has
   decayed.
4. Stop = 1.0 x ATR beyond the session extreme. Target = prior
   close (full gap fill) OR 2R, whichever is closer (prefer the
   structural target when it's tighter).

Risk
----
- 1.0x ATR stop. Scales with realized vol; tighter than spot to
  reflect the cleaner mean-reversion thesis on a finite gap.
- Tick-quantized exits to MBT's 5.0 USD tick.
- Single trade per session. The opportunity is the open; if it
  doesn't pay we walk away.

Status
------
research_candidate — defaults are CONSERVATIVE, not optimized.
TODO: walk-forward validate gap thresholds against historical MBT
data; the 0.3x / 1.0x ATR band is a reasonable starting point per
the gap_fill_strategy literature but MBT-specific thresholds may
differ.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


_MBT_TICK_SIZE: float = 5.0


@dataclass(frozen=True)
class MBTOvernightGapConfig:
    """Parameters for the MBT overnight-gap fade.

    Defaults are CONSERVATIVE; walk-forward validation must precede
    any promotion past paper-soak.
    """

    # Gap classification — both bounds matter (small gaps are noise,
    # large gaps are news-driven trends, not fades).
    min_gap_atr_mult: float = 0.3
    max_gap_atr_mult: float = 1.5

    # Session window
    rth_open_local: time = time(8, 30)
    rth_close_local: time = time(15, 0)
    timezone_name: str = "America/Chicago"
    # Number of post-open bars eligible for entry.
    entry_window_bars: int = 6
    # Flatten by this local time regardless.
    flatten_at_local: time = time(14, 50)

    # Risk / sizing
    atr_period: int = 14
    atr_stop_mult: float = 1.0
    rr_target: float = 2.0
    risk_per_trade_pct: float = 0.005
    # Minimum hours between prior session close and current bar to
    # treat as "overnight" (avoid weekend artifacts double-counting).
    min_session_gap_hours: float = 4.0

    # Hygiene
    max_trades_per_day: int = 1
    warmup_bars: int = 50

    # Direction
    allow_long: bool = True
    allow_short: bool = True


class MBTOvernightGapStrategy:
    """Single-purpose MBT overnight-gap fade at NY RTH open.

    Stateful across the bar stream. Carries:
    - last RTH-close price (anchor)
    - current session's RTH-open bar
    - whether a tradable gap exists today
    """

    def __init__(self, config: MBTOvernightGapConfig | None = None) -> None:
        self.cfg = config or MBTOvernightGapConfig()
        self._tz = ZoneInfo(self.cfg.timezone_name)
        self._last_rth_close: float | None = None
        self._last_rth_close_ts_date: object | None = None
        self._today_rth_open: float | None = None
        self._gap_side: str | None = None
        self._gap_size: float = 0.0
        self._post_open_bars: int = 0
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None
        # Audit
        self._n_gaps_detected: int = 0
        self._n_gaps_too_small: int = 0
        self._n_gaps_too_large: int = 0
        self._n_fired: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "gaps_detected": self._n_gaps_detected,
            "gaps_too_small": self._n_gaps_too_small,
            "gaps_too_large": self._n_gaps_too_large,
            "entries_fired": self._n_fired,
        }

    # -- helpers ----------------------------------------------------------

    def _local_time(self, bar: BarData) -> time:
        local_t = bar.timestamp.astimezone(self._tz).timetz()
        return time(local_t.hour, local_t.minute, local_t.second)

    def _in_session(self, bar: BarData) -> bool:
        local_only = self._local_time(bar)
        return self.cfg.rth_open_local <= local_only < self.cfg.rth_close_local

    def _is_rth_open_bar(self, bar: BarData) -> bool:
        local_only = self._local_time(bar)
        return (
            local_only.hour == self.cfg.rth_open_local.hour
            and local_only.minute == self.cfg.rth_open_local.minute
        )

    @staticmethod
    def _quantize_to_tick(price: float, tick: float) -> float:
        if tick <= 0.0:
            return price
        return round(price / tick) * tick

    def _atr(self, hist: list[BarData]) -> float:
        if not hist:
            return 0.0
        window = hist[-self.cfg.atr_period:]
        if not window:
            return 0.0
        return sum(b.high - b.low for b in window) / len(window)

    # -- main entry point ------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        bar_date = bar.timestamp.date()
        new_day = self._last_day != bar_date

        # ── New session detection ──────────────────────────────────────
        if new_day:
            # Roll session anchors
            self._last_day = bar_date
            self._trades_today = 0
            self._post_open_bars = 0
            self._gap_side = None
            self._gap_size = 0.0
            self._today_rth_open = None
            # The last bar of the prior in-memory session is our
            # "RTH close" anchor.
            if hist:
                self._last_rth_close = hist[-1].close

        self._bars_seen += 1
        in_sess = self._in_session(bar)

        # Detect RTH-open: first in-session bar after a non-session
        # gap of >= min_session_gap_hours since prior bar.
        if (
            in_sess
            and self._today_rth_open is None
            and (self._is_rth_open_bar(bar) or new_day)
        ):
            # Validate gap-of-time vs prior bar
            time_gap_ok = True
            if hist:
                dt_hours = (
                    bar.timestamp - hist[-1].timestamp
                ).total_seconds() / 3600.0
                if dt_hours < self.cfg.min_session_gap_hours:
                    time_gap_ok = False
            if time_gap_ok:
                self._today_rth_open = bar.open
                # Compute gap if we have an anchor
                if self._last_rth_close is not None:
                    atr = self._atr(hist)
                    if atr > 0.0:
                        gap_abs = abs(self._today_rth_open - self._last_rth_close)
                        if gap_abs < self.cfg.min_gap_atr_mult * atr:
                            self._n_gaps_too_small += 1
                        elif gap_abs > self.cfg.max_gap_atr_mult * atr:
                            self._n_gaps_too_large += 1
                        else:
                            self._n_gaps_detected += 1
                            self._gap_size = gap_abs
                            if self._today_rth_open > self._last_rth_close:
                                self._gap_side = "SELL"  # fade gap up
                            else:
                                self._gap_side = "BUY"  # fade gap down

        # Track post-open bar count for window enforcement
        if in_sess and self._today_rth_open is not None:
            self._post_open_bars += 1

        # ── Eligibility gates ─────────────────────────────────────────
        if self._bars_seen < self.cfg.warmup_bars:
            return None
        if not in_sess:
            return None
        if self._gap_side is None:
            return None
        if self._post_open_bars > self.cfg.entry_window_bars:
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        # Flatten time check
        if self._local_time(bar) >= self.cfg.flatten_at_local:
            return None

        side = self._gap_side
        if side == "BUY" and not self.cfg.allow_long:
            return None
        if side == "SELL" and not self.cfg.allow_short:
            return None

        # Confirmation: simple bar-direction match — the bar should
        # be moving in the fade direction (lower close than open
        # for SHORT; higher close than open for LONG).
        if side == "SELL" and bar.close >= bar.open:
            return None
        if side == "BUY" and bar.close <= bar.open:
            return None

        # Risk sizing
        atr = self._atr(hist)
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
        prior_close = self._last_rth_close

        if side == "BUY":
            raw_stop = entry - stop_dist
            # Prefer prior close as target if it's an ABOVE-entry value
            # (gap fill); else fall back to RR.
            if prior_close is not None and prior_close > entry:
                raw_target = prior_close
            else:
                raw_target = entry + self.cfg.rr_target * stop_dist
        else:
            raw_stop = entry + stop_dist
            if prior_close is not None and prior_close < entry:
                raw_target = prior_close
            else:
                raw_target = entry - self.cfg.rr_target * stop_dist

        stop = self._quantize_to_tick(raw_stop, _MBT_TICK_SIZE)
        target = self._quantize_to_tick(raw_target, _MBT_TICK_SIZE)
        # Defensive: ensure stop/target stay on correct side after
        # quantization for very small ATRs.
        if side == "BUY":
            if stop >= entry:
                stop = entry - _MBT_TICK_SIZE
            if target <= entry:
                target = entry + _MBT_TICK_SIZE
        else:
            if stop <= entry:
                stop = entry + _MBT_TICK_SIZE
            if target >= entry:
                target = entry - _MBT_TICK_SIZE

        from eta_engine.backtest.engine import _Open

        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        self._n_fired += 1
        # Mark as "consumed" so we don't re-fire even if window allows
        self._gap_side = None
        return _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=8.0, leverage=1.0,
            regime=f"mbt_overnight_gap_{side.lower()}_{self._gap_size:.0f}",
        )


# ---------------------------------------------------------------------------
# Preset
# ---------------------------------------------------------------------------


def mbt_overnight_gap_preset() -> MBTOvernightGapConfig:
    """Default research_candidate config for MBT overnight-gap fade.

    NOTE: defaults are CONSERVATIVE. Walk-forward validation
    against MBT historical data is required before promotion.
    """
    return MBTOvernightGapConfig(
        min_gap_atr_mult=0.3,
        max_gap_atr_mult=1.5,
        entry_window_bars=6,
        atr_period=14,
        atr_stop_mult=1.0,
        rr_target=2.0,
        risk_per_trade_pct=0.005,
        min_session_gap_hours=4.0,
        max_trades_per_day=1,
        warmup_bars=50,
    )
