"""
EVOLUTIONARY TRADING ALGO  //  strategies.mbt_overnight_gap_strategy
=====================================================================
MBT (CME Bitcoin Micro Future) -- overnight gap CONTINUATION at NY RTH
open.

Pivot history (2026-05-07)
--------------------------
This strategy was originally a gap-FADE (mean reversion: gap-up at NY
open -> SHORT, gap-down -> LONG). The 70d EDA on MBT 5m data
(2026-02-26 -> 2026-05-07, 49 RTH sessions) refuted the fade thesis:
  * Gap distribution: ~33% fill / ~33% extend / ~33% no-move.
  * Large gaps (>2%) extended (continued) at rates higher than fill.
  * Same-RTH gap-fill rate on >1.0xATR gaps was ~0%.
The mean-reversion edge is dead. The file is preserved (and the
class name is preserved) but the trade direction is REVERSED into a
continuation thesis. Any code that imports MBTOvernightGapStrategy
will keep working; the side it returns is now the opposite of the
old build.

Concept (post-pivot)
--------------------
CME bitcoin futures trade nearly 24h. Asia-hours flow into thin books
moves price; the NY RTH open does NOT reliably mean-revert that move
on MBT -- when the gap is large enough to clear the noise floor, NY
liquidity tends to extend the move rather than fade it. The trade is
to align with the gap on the open and ride the continuation.

Mechanic
--------
1. Track the most-recent RTH close as the "anchor". When a new
   RTH session begins, compute the overnight gap = (RTH-open price)
   minus prior-RTH-close.
2. If the gap exceeds ``min_gap_atr_mult`` x ATR but is below
   ``max_gap_atr_mult`` (excessive gaps are news-driven, often
   exhaust quickly on retest):
   - Gap up -> LONG at NY open (continuation, NOT fade).
   - Gap down -> SHORT at NY open (continuation, NOT fade).
3. Confirmation: the entry bar must close in the CONTINUATION
   direction (close > open for a long after gap-up; close < open for
   a short after gap-down). The legacy fade code required the bar to
   close in the FADE direction -- that rule was filtering out the
   exact bars on which the new thesis works, so it's been reversed.
4. Trade window: only the first ``entry_window_bars`` bars after
   the RTH open are eligible.
5. Stop = 1.0 x ATR. Target = 2R. Prior-close (full gap fill) is no
   longer used as a structural target -- under the continuation
   thesis we are explicitly NOT trying to revisit prior close.

Risk
----
- 1.0x ATR stop. Scales with realized vol.
- 2R RR target (in the direction of the gap).
- Tick-quantized exits to MBT's 5.0 USD tick.
- Single trade per session.
- Min-gap floor raised from 0.3xATR -> 1.0xATR per EDA (smaller gaps
  are noise; the continuation edge only shows on real gaps).

Status
------
research_candidate. The pivot from fade -> continuation is a thesis
inversion derived from a single 49-session in-sample look. EDA
explicitly noted: 49 sessions is NOT walk-forward validation. The
hypothesis that "large gaps continue" survived an in-sample peek
and may not survive walk-forward. Walk-forward + Monte Carlo +
operator-signed kill criteria gate MUST clear before promotion past
paper-soak.
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
# CME Micro Bitcoin: 0.10 BTC per contract. $1 of price move = $0.10 P&L.
# Sizing math MUST multiply stop_dist by this to compute correct contract count.
_MBT_POINT_VALUE: float = 0.10


@dataclass(frozen=True)
class MBTOvernightGapConfig:
    """Parameters for the MBT overnight-gap CONTINUATION trade.

    Defaults reflect the post-pivot continuation thesis. EDA-derived
    where called out; otherwise CONSERVATIVE. Walk-forward validation
    must precede any promotion past paper-soak.
    """

    # Gap classification -- both bounds matter. Floor was raised from
    # the legacy fade's 0.3xATR to 1.0xATR per EDA: gaps below 1xATR
    # are noise; the continuation edge only shows on real gaps.
    # Ceiling stays at 1.5xATR -- over-large gaps are news-driven and
    # often exhaust on retest.
    min_gap_atr_mult: float = 1.0
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
        return local_only.hour == self.cfg.rth_open_local.hour and local_only.minute == self.cfg.rth_open_local.minute

    @staticmethod
    def _quantize_to_tick(price: float, tick: float) -> float:
        if tick <= 0.0:
            return price
        return round(price / tick) * tick

    def _atr(self, hist: list[BarData]) -> float:
        if not hist:
            return 0.0
        window = hist[-self.cfg.atr_period :]
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
        # Day boundary anchored to America/Chicago (CME local).
        bar_date = bar.timestamp.astimezone(self._tz).date()
        new_day = self._last_day != bar_date

        # --- New session detection ---
        if new_day:
            # Roll session anchors
            self._last_day = bar_date
            self._trades_today = 0
            self._post_open_bars = 0
            self._gap_side = None
            self._gap_size = 0.0
            self._today_rth_open = None
            # The "RTH close" anchor must come from a bar that was inside
            # CME RTH (08:30-15:00 CT) on the PRIOR day. If the bar feed
            # includes ETH (extended-hours) bars between sessions, hist[-1]
            # is an overnight bar, not a true RTH close -- using it would
            # silently corrupt every gap measurement.
            #
            # Walk hist backwards looking for the most recent bar that
            # was: (a) on a different (prior) Chicago-local date AND
            # (b) inside RTH. If none found within a reasonable lookback,
            # leave the anchor None (gap detection skipped this session).
            if hist:
                anchor: BarData | None = None
                for prior in reversed(hist[-200:]):  # <=200 bars lookback
                    prior_date = prior.timestamp.astimezone(self._tz).date()
                    if prior_date == bar_date:
                        continue  # same-day bar, keep walking
                    if self._in_session(prior):
                        anchor = prior
                        break
                if anchor is not None:
                    self._last_rth_close = anchor.close

        self._bars_seen += 1
        in_sess = self._in_session(bar)

        # Detect RTH-open: first in-session bar after a non-session
        # gap of >= min_session_gap_hours since prior bar.
        if in_sess and self._today_rth_open is None and (self._is_rth_open_bar(bar) or new_day):
            # Validate gap-of-time vs prior bar
            time_gap_ok = True
            if hist:
                dt_hours = (bar.timestamp - hist[-1].timestamp).total_seconds() / 3600.0
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
                            # CONTINUATION (post-2026-05-07 pivot):
                            # gap-up -> LONG to ride the move;
                            # gap-down -> SHORT. The legacy code
                            # had these reversed (fade thesis).
                            if self._today_rth_open > self._last_rth_close:
                                self._gap_side = "BUY"  # continue gap up
                            else:
                                self._gap_side = "SELL"  # continue gap down

        # Track post-open bar count for window enforcement
        if in_sess and self._today_rth_open is not None:
            self._post_open_bars += 1

        # --- Eligibility gates ---
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

        # Confirmation: bar must close in the CONTINUATION direction.
        # Post-pivot thesis: a gap-up that sees a green entry-bar
        # close (close > open) is the move acknowledging itself; a
        # gap-up with a red bar is the start of an exhaustion fade
        # we explicitly do NOT want to be long. (Legacy fade build
        # required the OPPOSITE bar direction; that rule was filtering
        # out the exact bars on which the new thesis works.)
        if side == "BUY" and bar.close <= bar.open:
            return None
        if side == "SELL" and bar.close >= bar.open:
            return None

        # Risk sizing
        atr = self._atr(hist)
        if atr <= 0.0:
            return None
        stop_dist = self.cfg.atr_stop_mult * atr
        if stop_dist <= 0.0:
            return None
        risk_usd = equity * self.cfg.risk_per_trade_pct
        # qty = $risk / ($-per-contract for stop_dist of price)
        # $-per-contract = stop_dist x point_value. MBT pv=0.10 => without
        # the multiplier the strategy would size 10x larger than intended.
        qty = risk_usd / (stop_dist * _MBT_POINT_VALUE)
        if qty <= 0.0:
            return None

        entry = bar.close

        # Continuation thesis: the target is always RR-based in the
        # direction of the gap. The legacy fade code preferred prior-
        # close (a "gap fill" target); under continuation we are
        # explicitly NOT trying to revisit prior close, so that branch
        # is dropped.
        if side == "BUY":
            raw_stop = entry - stop_dist
            raw_target = entry + self.cfg.rr_target * stop_dist
        else:
            raw_stop = entry + stop_dist
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
            entry_bar=bar,
            side=side,
            qty=qty,
            entry_price=entry,
            stop=stop,
            target=target,
            risk_usd=risk_usd,
            confluence=8.0,
            leverage=1.0,
            regime=(f"mbt_overnight_gap_continuation_{side.lower()}_{self._gap_size:.0f}"),
        )


# ---------------------------------------------------------------------------
# Preset
# ---------------------------------------------------------------------------


def mbt_overnight_gap_preset() -> MBTOvernightGapConfig:
    """Default research_candidate config for MBT overnight-gap
    CONTINUATION trade (post-2026-05-07 pivot from fade thesis).

    NOTE: 49-session in-sample EDA is NOT walk-forward validation.
    The continuation thesis is a hypothesis derived from a single
    in-sample look. Walk-forward + Monte Carlo + operator-signed
    kill criteria gate MUST clear before promotion past paper-soak.
    """
    return MBTOvernightGapConfig(
        # EDA-derived: 1.0xATR is the floor below which gaps are
        # noise; the continuation edge only shows on real gaps.
        min_gap_atr_mult=1.0,
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
