"""
EVOLUTIONARY TRADING ALGO  //  strategies.anchor_sweep_strategy
==================================================================
Named-anchor variant of the sweep_reclaim mechanic for MNQ/NQ
index futures.

Why a separate strategy
-----------------------
The base ``SweepReclaimStrategy`` uses a rolling N-bar lookback to
identify "liquidity zones" (recent highs/lows). On crypto 1h that
produces real signal — daily wick extremes line up well with N-bar
extremes. On US index futures the rolling-N approach is the WRONG
abstraction: institutions stop-hunt at FIXED, named levels, not at
arbitrary 100-minute extremes. The key levels are:

  PDH / PDL : prior trading day's RTH high/low (09:30-16:00 ET)
  PMH / PML : today's premarket high/low      (04:00-09:30 ET)
  ONH / ONL : today's overnight high/low      (18:00 prev → 04:00)

Mechanic — identical to base sweep_reclaim, anchored differently
----------------------------------------------------------------
1. Maintain the named-anchor state machine. At each ET session
   boundary the appropriate extremes are frozen.
2. On each bar, check if ``bar.high`` or ``bar.low`` pierced ANY
   active named anchor AND ``bar.close`` reclaimed back inside.
3. Direction:  sweep-of-high → SHORT (false breakout)
                 sweep-of-low  → LONG  (false breakdown)
4. Stop: wick-aware buffer — same ``max(0.5*wick_depth, 0.25*atr)``
   pattern as the base sweep_reclaim post-fix.
5. Target: the previous opposite anchor (so a PDH sweep aims at
   PDL; a PDL sweep aims at PDH). When the natural opposite anchor
   isn't yet defined, fall back to a 2R RR target.

This stays mechanically simple — no scorecard, no regime gate.
A higher-level wrapper (sage_daily_gated, regime gated, etc.) can
filter directional bias if the operator wants it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time as _dtime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


_NY_TZ = ZoneInfo("America/New_York")

# Session window edges in ET. Inclusive lower / exclusive upper.
_PREMARKET_OPEN = _dtime(4, 0)
_RTH_OPEN = _dtime(9, 30)
_RTH_CLOSE = _dtime(16, 0)
_ON_START = _dtime(18, 0)


@dataclass(frozen=True)
class AnchorSweepConfig:
    """Knobs for the named-anchor sweep+reclaim strategy."""

    # Which named anchors are armed for sweep detection. Subset of
    # the canonical six. Operators can disable, e.g., overnight
    # extremes if they only care about cash-session levels.
    anchor_set: tuple[str, ...] = ("PDH", "PDL", "PMH", "PML", "ONH", "ONL")

    # Min wick portion of the bar's full range that must extend
    # beyond the swept anchor. Lower than the base (0.40) because
    # named anchors are higher-quality liquidity.
    min_wick_pct: float = 0.50

    # Volume z-score floor.
    volume_z_lookback: int = 20
    min_volume_z: float = 0.5

    # Reclaim must occur within N bars (1 = same bar as the sweep).
    reclaim_window: int = 1

    # Risk
    atr_period: int = 14
    atr_stop_mult: float = 1.0
    rr_target: float = 2.0  # used as fallback when no opposite anchor exists
    risk_per_trade_pct: float = 0.005

    # Hygiene
    max_trades_per_day: int = 3
    min_bars_between_trades: int = 12

    # Direction toggles
    allow_long: bool = True
    allow_short: bool = True


@dataclass
class _AnchorState:
    """Live + frozen anchor levels for a single trading day in ET."""

    # Frozen anchors — read-only for sweep detection
    pdh: float | None = None
    pdl: float | None = None
    pmh: float | None = None
    pml: float | None = None
    onh: float | None = None
    onl: float | None = None

    # Live (currently being filled) buckets
    rth_high_today: float | None = None
    rth_low_today: float | None = None
    pm_high_today: float | None = None
    pm_low_today: float | None = None
    on_high_today: float | None = None
    on_low_today: float | None = None

    # Track current date (ET) so we know when to roll
    current_et_date: object | None = None

    # Track which session the previous bar belonged to so we fire
    # the correct boundary transition exactly once.
    last_session_bucket: str | None = None


class AnchorSweepStrategy:
    """Named-anchor sweep+reclaim for MNQ/NQ futures."""

    def __init__(self, config: AnchorSweepConfig | None = None) -> None:
        self.cfg = config or AnchorSweepConfig()
        self._state = _AnchorState()
        # Volume window for z-scoring
        self._volume_window: list[float] = []
        # Pending sweeps awaiting reclaim. Each:
        # (anchor_name, side, level, bar_idx_when_swept, opposite_target)
        # opposite_target may be None — fallback 2R will be used.
        self._pending: list[tuple[str, str, float, int, float | None]] = []
        self._bars_seen = 0
        self._last_entry_idx: int | None = None
        self._trades_today = 0
        self._trade_count_date: object | None = None
        # Audit
        self._n_long_sweeps_seen = 0
        self._n_short_sweeps_seen = 0
        self._n_reclaims_fired = 0
        self._n_wick_quality_rejects = 0
        self._n_volume_quality_rejects = 0
        self._n_reclaim_window_expired = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "long_sweeps_seen": self._n_long_sweeps_seen,
            "short_sweeps_seen": self._n_short_sweeps_seen,
            "reclaims_fired": self._n_reclaims_fired,
            "wick_quality_rejects": self._n_wick_quality_rejects,
            "volume_quality_rejects": self._n_volume_quality_rejects,
            "reclaim_window_expired": self._n_reclaim_window_expired,
        }

    # -- session classification -------------------------------------------

    @staticmethod
    def _bucket_for(et_time: _dtime) -> str:
        """Map an ET time-of-day to a session bucket label.

        Buckets:
          ``ON``  : 18:00 (prev day) - 04:00 ET (overnight)
          ``PM``  : 04:00 - 09:30 ET (premarket)
          ``RTH`` : 09:30 - 16:00 ET (regular hours)
          ``POST``: 16:00 - 18:00 ET (post-close gap; bars are tracked
                   into ON for the next day's overnight bucket)
        """
        if _RTH_OPEN <= et_time < _RTH_CLOSE:
            return "RTH"
        if _PREMARKET_OPEN <= et_time < _RTH_OPEN:
            return "PM"
        if _RTH_CLOSE <= et_time < _ON_START:
            return "POST"
        # 18:00 - 23:59:59 OR 00:00 - 03:59:59 → overnight
        return "ON"

    # -- anchor refresh ---------------------------------------------------

    def _update_anchors(self, bar: BarData) -> None:
        """Roll the state machine forward one bar."""
        local_ts = bar.timestamp.astimezone(_NY_TZ)
        local_time = local_ts.time()
        et_date = local_ts.date()
        bucket = self._bucket_for(local_time)
        st = self._state

        # New ET day — reset trade-counter and shift "today's" RTH
        # frozen extremes into PDH/PDL slots (carry-forward from
        # yesterday's RTH session).
        if st.current_et_date != et_date:
            # Yesterday's RTH high/low becomes today's PDH/PDL.
            # If we never saw an RTH session for the prior calendar
            # day (e.g. weekend), keep the existing PDH/PDL — they
            # remain the most recent prior-day extremes.
            if st.rth_high_today is not None and st.rth_low_today is not None:
                st.pdh = st.rth_high_today
                st.pdl = st.rth_low_today
            st.rth_high_today = None
            st.rth_low_today = None
            # Premarket / overnight buckets reset for the new day.
            # (overnight technically started yesterday at 18:00 — but
            # we accumulate into on_*_today as bars arrive)
            st.pmh = None
            st.pml = None
            st.pm_high_today = None
            st.pm_low_today = None
            # Trade counter resets per ET date
            self._trade_count_date = et_date
            self._trades_today = 0
            st.current_et_date = et_date

        # Boundary transitions inside the same ET date:
        prev_bucket = st.last_session_bucket
        if prev_bucket is not None and prev_bucket != bucket:
            if prev_bucket == "PM" and bucket == "RTH":
                # 09:30 transition: freeze premarket extremes
                if st.pm_high_today is not None and st.pm_low_today is not None:
                    st.pmh = st.pm_high_today
                    st.pml = st.pm_low_today
            elif prev_bucket == "ON" and bucket == "PM":
                # 04:00 transition: freeze overnight extremes
                if st.on_high_today is not None and st.on_low_today is not None:
                    st.onh = st.on_high_today
                    st.onl = st.on_low_today
                # ON bucket is now closed for the day; reset accumulator
                st.on_high_today = None
                st.on_low_today = None
            elif prev_bucket == "RTH" and bucket in {"POST", "ON"}:
                # 16:00 transition: today's RTH high/low becomes the
                # *frozen* "today RTH" — but PDH/PDL only become
                # populated on the next ET date roll.
                # No live action needed here; the next-day roll
                # picks up rth_*_today as PDH/PDL.
                pass

        # Accumulate into the appropriate live bucket
        if bucket == "RTH":
            if st.rth_high_today is None or bar.high > st.rth_high_today:
                st.rth_high_today = bar.high
            if st.rth_low_today is None or bar.low < st.rth_low_today:
                st.rth_low_today = bar.low
        elif bucket == "PM":
            if st.pm_high_today is None or bar.high > st.pm_high_today:
                st.pm_high_today = bar.high
            if st.pm_low_today is None or bar.low < st.pm_low_today:
                st.pm_low_today = bar.low
        elif bucket in {"ON", "POST"}:
            # POST (16:00-18:00) bars contribute to the upcoming ON
            # bucket — institutionally these print into the same
            # overnight liquidity pool.
            if st.on_high_today is None or bar.high > st.on_high_today:
                st.on_high_today = bar.high
            if st.on_low_today is None or bar.low < st.on_low_today:
                st.on_low_today = bar.low

        st.last_session_bucket = bucket

    # -- anchor accessors --------------------------------------------------

    def _active_high_anchors(self) -> dict[str, float]:
        """Return {name: level} for currently-frozen HIGH anchors in cfg.anchor_set."""
        out: dict[str, float] = {}
        st = self._state
        if "PDH" in self.cfg.anchor_set and st.pdh is not None:
            out["PDH"] = st.pdh
        if "PMH" in self.cfg.anchor_set and st.pmh is not None:
            out["PMH"] = st.pmh
        if "ONH" in self.cfg.anchor_set and st.onh is not None:
            out["ONH"] = st.onh
        return out

    def _active_low_anchors(self) -> dict[str, float]:
        out: dict[str, float] = {}
        st = self._state
        if "PDL" in self.cfg.anchor_set and st.pdl is not None:
            out["PDL"] = st.pdl
        if "PML" in self.cfg.anchor_set and st.pml is not None:
            out["PML"] = st.pml
        if "ONL" in self.cfg.anchor_set and st.onl is not None:
            out["ONL"] = st.onl
        return out

    def _opposite_target(self, anchor_name: str) -> float | None:
        """Get the natural opposite-anchor target for a given sweep.

        Sweep PDH (short) → target PDL.
        Sweep PML (long)  → target PMH. (etc.)
        Falls back to None when the opposite anchor isn't defined yet.
        """
        # Map highs to lows and vice-versa within the same session
        st = self._state
        opposites: dict[str, float | None] = {
            "PDH": st.pdl,
            "PDL": st.pdh,
            "PMH": st.pml,
            "PML": st.pmh,
            "ONH": st.onl,
            "ONL": st.onh,
        }
        return opposites.get(anchor_name)

    # -- sweep / reclaim detection ----------------------------------------

    def _detect_sweep(self, bar: BarData) -> None:
        """Check if THIS bar swept any active named anchor."""
        bar_range = max(bar.high - bar.low, 1e-9)

        # SHORT sweeps: high pierced a HIGH anchor
        if self.cfg.allow_short:
            for name, level in self._active_high_anchors().items():
                if bar.high > level:
                    wick_pct = (bar.high - level) / bar_range
                    if wick_pct >= self.cfg.min_wick_pct:
                        target = self._opposite_target(name)
                        self._pending.append(
                            (name, "SELL", level, self._bars_seen, target),
                        )
                        self._n_short_sweeps_seen += 1
                    else:
                        self._n_wick_quality_rejects += 1

        # LONG sweeps: low pierced a LOW anchor
        if self.cfg.allow_long:
            for name, level in self._active_low_anchors().items():
                if bar.low < level:
                    wick_pct = (level - bar.low) / bar_range
                    if wick_pct >= self.cfg.min_wick_pct:
                        target = self._opposite_target(name)
                        self._pending.append(
                            (name, "BUY", level, self._bars_seen, target),
                        )
                        self._n_long_sweeps_seen += 1
                    else:
                        self._n_wick_quality_rejects += 1

    def _check_reclaim(self, bar: BarData) -> tuple[str, str, float, float | None] | None:
        """Look for a pending sweep that THIS bar reclaimed.

        Returns ``(anchor_name, side, level, opposite_target_price)`` or
        ``None``. Expired pending sweeps are dropped.
        """
        valid: list[tuple[str, str, float, int, float | None]] = []
        winner: tuple[str, str, float, float | None] | None = None
        for name, side, level, sweep_idx, opp in self._pending:
            age = self._bars_seen - sweep_idx
            if age > self.cfg.reclaim_window:
                self._n_reclaim_window_expired += 1
                continue
            if winner is None:
                if side == "BUY" and bar.close > level:
                    winner = (name, "BUY", level, opp)
                    continue
                if side == "SELL" and bar.close < level:
                    winner = (name, "SELL", level, opp)
                    continue
            valid.append((name, side, level, sweep_idx, opp))
        self._pending = valid
        return winner

    def _volume_z_score(self, bar: BarData) -> float:
        if len(self._volume_window) < self.cfg.volume_z_lookback:
            return 0.0
        vols = self._volume_window[-self.cfg.volume_z_lookback:]
        mean = sum(vols) / len(vols)
        var = sum((v - mean) ** 2 for v in vols) / len(vols)
        std = var ** 0.5
        if std <= 0.0:
            return 0.0
        return (bar.volume - mean) / std

    # -- main entry --------------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        self._bars_seen += 1
        self._volume_window.append(bar.volume)
        if len(self._volume_window) > self.cfg.volume_z_lookback * 4:
            # Cap memory growth — we only need a rolling window
            self._volume_window = self._volume_window[-self.cfg.volume_z_lookback * 2:]

        # Update anchor state machine BEFORE detection (so today's
        # in-progress RTH high doesn't get treated as PDH)
        self._update_anchors(bar)

        # Detect a fresh sweep on this bar (uses the frozen anchors —
        # update_anchors only mutates LIVE buckets, never the frozen
        # ones, between session boundaries)
        self._detect_sweep(bar)

        # Reclaim check — fires on the same bar as the sweep when
        # reclaim_window=1 (a 1-bar wick + close-back-inside pattern)
        winner = self._check_reclaim(bar)
        if winner is None:
            return None

        # Per-day trade limit
        local_date = bar.timestamp.astimezone(_NY_TZ).date()
        if self._trade_count_date != local_date:
            self._trade_count_date = local_date
            self._trades_today = 0
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None

        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx)
            < self.cfg.min_bars_between_trades
        ):
            return None

        anchor_name, side, swept_level, opp_target = winner

        # Volume confirmation
        if self.cfg.min_volume_z > 0:
            vz = self._volume_z_score(bar)
            if vz < self.cfg.min_volume_z:
                self._n_volume_quality_rejects += 1
                return None

        # ATR for stop sizing
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

        # Wick-aware stop (mirrors the base sweep_reclaim post-fix)
        if side == "BUY":
            wick_depth = max(entry - bar.low, 0.0)
            wick_buffer = max(0.5 * wick_depth, 0.25 * atr)
            structure_stop = bar.low - wick_buffer
            atr_stop = entry - stop_dist
            stop = min(structure_stop, atr_stop)
            stop_dist_actual = entry - stop
            # Prefer named-opposite-anchor target, fallback to RR
            if opp_target is not None and opp_target > entry:
                target = opp_target
            else:
                target = entry + self.cfg.rr_target * stop_dist_actual
        else:  # SELL
            wick_depth = max(bar.high - entry, 0.0)
            wick_buffer = max(0.5 * wick_depth, 0.25 * atr)
            structure_stop = bar.high + wick_buffer
            atr_stop = entry + stop_dist
            stop = max(structure_stop, atr_stop)
            stop_dist_actual = stop - entry
            if opp_target is not None and opp_target < entry:
                target = opp_target
            else:
                target = entry - self.cfg.rr_target * stop_dist_actual

        # If the opposite-anchor target lands on the wrong side of
        # entry (degenerate setup, e.g. PDL above current price on a
        # PDH sweep — rare but possible during gap days), fall back
        # to the RR target.
        if side == "BUY" and target <= entry:
            target = entry + self.cfg.rr_target * stop_dist_actual
        if side == "SELL" and target >= entry:
            target = entry - self.cfg.rr_target * stop_dist_actual

        # Cap absurdly-high RR.  When the opposite anchor is far away
        # but the sweep wick was tight, target/stop_dist can hit RR=50+
        # which the signal_validator rejects as `rr_absurd`.  Cap at
        # RR=10 — still aggressive but tradeable, and the strategy
        # will exit at the cap target instead of being rejected.
        max_rr = 10.0
        implied_rr = abs(target - entry) / max(stop_dist_actual, 1e-9)
        if implied_rr > max_rr:
            target = entry + max_rr * stop_dist_actual if side == "BUY" else entry - max_rr * stop_dist_actual

        from eta_engine.backtest.engine import _Open

        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        self._n_reclaims_fired += 1
        return _Open(
            entry_bar=bar,
            side=side,
            qty=qty,
            entry_price=entry,
            stop=stop,
            target=target,
            risk_usd=risk_usd,
            confluence=10.0,
            leverage=1.0,
            regime=f"anchor_sweep_{side.lower()}_{anchor_name}",
        )


# ---------------------------------------------------------------------------
# Asset-class presets
# ---------------------------------------------------------------------------


def mnq_anchor_sweep_preset() -> AnchorSweepConfig:
    """MNQ 5m intraday — full named-anchor set.

    Paper-soak v2 tuning (2026-05-06): atr_stop_mult 1.0→1.5 (22.9% WR
    was largely stops hit by noise around named anchors — wider stop
    gives the reclaim thesis room), reclaim_window 1→2 (one bar often
    not enough for close-back-inside on MNQ 5m sweep wicks).
    """
    return AnchorSweepConfig(
        anchor_set=("PDH", "PDL", "PMH", "PML", "ONH", "ONL"),
        min_wick_pct=0.50,
        volume_z_lookback=20,
        min_volume_z=0.5,
        reclaim_window=2,
        atr_period=14,
        atr_stop_mult=1.5,
        rr_target=2.0,
        risk_per_trade_pct=0.005,
        max_trades_per_day=3,
        min_bars_between_trades=12,
    )


def nq_anchor_sweep_preset() -> AnchorSweepConfig:
    """NQ 5m intraday — same Nasdaq-100 underlying, identical mechanic.

    Sized via ``risk_per_trade_pct * equity / stop_distance``; contract
    size differences (NQ $20/pt vs MNQ $2/pt) are absorbed by ``qty``.
    Defined as a separate factory so future NQ-specific tuning has a
    clean home.
    """
    return AnchorSweepConfig(
        anchor_set=("PDH", "PDL", "PMH", "PML", "ONH", "ONL"),
        min_wick_pct=0.50,
        volume_z_lookback=20,
        min_volume_z=0.5,
        reclaim_window=1,
        atr_period=14,
        atr_stop_mult=1.0,
        rr_target=2.0,
        risk_per_trade_pct=0.005,
        max_trades_per_day=3,
        min_bars_between_trades=12,
    )


__all__ = [
    "AnchorSweepConfig",
    "AnchorSweepStrategy",
    "mnq_anchor_sweep_preset",
    "nq_anchor_sweep_preset",
]
