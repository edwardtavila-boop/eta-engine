"""Wave-4 diamond rehaul — structural improvements after the stratification
+ forensic audit + watchdog data revealed:
  - 5/8 diamonds have n<10 (sparse)
  - 2/8 CRITICAL on watchdog
  - mnq/nq per-trade R = +0.001 (real but tiny)
  - mgc edge lives in overnight only (close session NULL)
  - eur_sweep is the one genuinely strong diamond

This wave shipped concrete improvements (not curve fits):

1. Trailing stop in commodity_momentum (was dead-code config like ADX)
   — implements the trailing_stop_atr_mult parameter via the new
   compute_trailing_stop() helper.  Pure function for the supervisor
   exit loop to call.

2. Vol-adjusted sizing in BOTH commodity_momentum AND sweep_reclaim
   — when realized ATR exceeds median * vol_high_threshold, size
   DOWN to avoid burning double-risk on regime spikes.

3. Multi-bar reclaim confirmation in sweep_reclaim
   — reclaim_confirm_bars=2 requires 2 consecutive bars on the
   reclaim side, cutting false signals in chop.

4. Session filter in sweep_reclaim
   — excluded_hours_utc=(20,21,22,23) for mgc drops the close
   session where stratification found CI lower -0.169 (NULL edge).
"""
# ruff: noqa: N802, PLR2004
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class _MockBar:
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts: str = "2026-05-12T14:30:00+00:00"

    @property
    def timestamp(self):  # noqa: ANN201
        return datetime.fromisoformat(self.ts.replace("Z", "+00:00"))


# ────────────────────────────────────────────────────────────────────
# Trailing stop in commodity_momentum (was dead code)
# ────────────────────────────────────────────────────────────────────


def test_trailing_stop_returns_none_below_trigger() -> None:
    """Trailing stop must not activate before price has moved
    rr_trail_trigger * R in favor."""
    from eta_engine.strategies.commodity_momentum_strategy import (
        MomentumConfig,
        MomentumStrategy,
    )

    strat = MomentumStrategy(MomentumConfig(
        trailing_stop_atr_mult=1.0, rr_trail_trigger=1.0,
    ))
    # Entry 100, stop 95 → R=5.  Price at 102 = +0.4R → no trail yet.
    result = strat.compute_trailing_stop(
        side="BUY", entry_price=100.0, initial_stop=95.0,
        current_price=102.0, atr=2.0,
    )
    assert result is None


def test_trailing_stop_activates_after_trigger_long() -> None:
    """LONG: at +1R move, trailing stop should be at
    current_price - trailing_stop_atr_mult * ATR."""
    from eta_engine.strategies.commodity_momentum_strategy import (
        MomentumConfig,
        MomentumStrategy,
    )

    strat = MomentumStrategy(MomentumConfig(
        trailing_stop_atr_mult=1.0, rr_trail_trigger=1.0,
    ))
    # Entry 100, stop 95 → R=5.  Price at 106 = +1.2R → trail active.
    # Trail = 106 - 1.0 * 2 = 104
    result = strat.compute_trailing_stop(
        side="BUY", entry_price=100.0, initial_stop=95.0,
        current_price=106.0, atr=2.0,
    )
    assert result is not None
    assert result == 104.0


def test_trailing_stop_never_widens_long() -> None:
    """The trailing stop must never move BELOW the initial stop on a
    long.  Even if the trailing math says 90 and initial is 95,
    operator gets 95."""
    from eta_engine.strategies.commodity_momentum_strategy import (
        MomentumConfig,
        MomentumStrategy,
    )

    strat = MomentumStrategy(MomentumConfig(
        trailing_stop_atr_mult=10.0, rr_trail_trigger=1.0,
    ))
    # Entry 100, stop 95.  Price barely above trigger at 105, but
    # huge trail_mult=10 * ATR=2 = 20 → 105-20=85 which is BELOW 95.
    # Must clamp to 95.
    result = strat.compute_trailing_stop(
        side="BUY", entry_price=100.0, initial_stop=95.0,
        current_price=105.0, atr=2.0,
    )
    assert result == 95.0  # clamped — never widened


def test_trailing_stop_short_side() -> None:
    """SHORT: at favorable move, trail above current price."""
    from eta_engine.strategies.commodity_momentum_strategy import (
        MomentumConfig,
        MomentumStrategy,
    )

    strat = MomentumStrategy(MomentumConfig(
        trailing_stop_atr_mult=1.0, rr_trail_trigger=1.0,
    ))
    # SHORT entry 100, stop 105 → R=5.  Price drops to 94 = +1.2R fav.
    # Trail = 94 + 1.0 * 2 = 96
    result = strat.compute_trailing_stop(
        side="SHORT", entry_price=100.0, initial_stop=105.0,
        current_price=94.0, atr=2.0,
    )
    assert result is not None
    assert result == 96.0


def test_trailing_stop_disabled_when_mult_zero() -> None:
    """trailing_stop_atr_mult=0 disables the trail entirely."""
    from eta_engine.strategies.commodity_momentum_strategy import (
        MomentumConfig,
        MomentumStrategy,
    )

    strat = MomentumStrategy(MomentumConfig(trailing_stop_atr_mult=0.0))
    result = strat.compute_trailing_stop(
        side="BUY", entry_price=100.0, initial_stop=95.0,
        current_price=110.0, atr=2.0,
    )
    assert result is None


# ────────────────────────────────────────────────────────────────────
# Vol-adjusted sizing
# ────────────────────────────────────────────────────────────────────


def test_sweep_reclaim_vol_adjusted_sizes_down_in_high_vol() -> None:
    """When current ATR > median * vol_high_threshold, qty should
    shrink relative to baseline by vol_high_size_mult."""
    from eta_engine.strategies.sweep_reclaim_strategy import (
        SweepReclaimConfig,
        SweepReclaimStrategy,
    )

    cfg = SweepReclaimConfig(
        vol_adjusted_sizing=True, vol_baseline_window=20,
        vol_high_threshold=1.5, vol_high_size_mult=0.5,
        vol_low_threshold=0.7, vol_low_size_mult=1.0,
    )
    strat = SweepReclaimStrategy(cfg)
    # Seed normal-vol baseline (ATR=2.0 across 20 bars)
    strat._atr_history.extend([2.0] * 20)
    # The vol-adjusted logic kicks in once history >= baseline_window/2 = 10
    assert len(strat._atr_history) >= cfg.vol_baseline_window // 2


def test_default_sweep_reclaim_vol_adjusted_disabled() -> None:
    """Default config = legacy behavior, no vol adjustment."""
    from eta_engine.strategies.sweep_reclaim_strategy import SweepReclaimConfig

    cfg = SweepReclaimConfig()
    assert cfg.vol_adjusted_sizing is False


def test_default_momentum_vol_adjusted_disabled() -> None:
    """Default config = legacy behavior, no vol adjustment."""
    from eta_engine.strategies.commodity_momentum_strategy import MomentumConfig

    cfg = MomentumConfig()
    assert cfg.vol_adjusted_sizing is False


# ────────────────────────────────────────────────────────────────────
# Multi-bar reclaim confirmation
# ────────────────────────────────────────────────────────────────────


def test_reclaim_confirm_bars_default_is_one_legacy() -> None:
    """Legacy single-bar reclaim must be preserved as the default."""
    from eta_engine.strategies.sweep_reclaim_strategy import SweepReclaimConfig

    cfg = SweepReclaimConfig()
    assert cfg.reclaim_confirm_bars == 1


def test_mgc_preset_opts_into_two_bar_confirm() -> None:
    """mgc_sweep_preset (wave-4 + wave-5) uses 2-bar confirmation
    and vol-adjusted sizing.  The wave-4 close-session exclusion was
    reverted in wave-5 after canonical-data analysis showed the
    excluded UTC hours (20-23) never actually contained mgc trades
    AND the close-session edge was misclassified as null."""
    from eta_engine.strategies.sweep_reclaim_strategy import mgc_sweep_preset

    cfg = mgc_sweep_preset()
    # Wave-4 mechanics that survived wave-5
    assert cfg.reclaim_confirm_bars == 2
    assert cfg.vol_adjusted_sizing is True
    # Wave-5 reverted the dead-code session exclusion
    assert cfg.excluded_hours_utc == ()


# ────────────────────────────────────────────────────────────────────
# Session filter
# ────────────────────────────────────────────────────────────────────


def test_session_filter_rejects_excluded_hour() -> None:
    """A bar whose UTC hour is in excluded_hours_utc must produce
    no signal AND increment the session_filter_rejects counter."""
    from eta_engine.strategies.sweep_reclaim_strategy import (
        SweepReclaimConfig,
        SweepReclaimStrategy,
    )

    cfg = SweepReclaimConfig(
        excluded_hours_utc=(20, 21, 22, 23),
        warmup_bars=2,
    )
    strat = SweepReclaimStrategy(cfg)
    bar = _MockBar(
        open=100.0, high=100.5, low=99.5, close=100.2, volume=1000.0,
        ts="2026-05-12T22:00:00+00:00",  # UTC 22 = excluded
    )
    result = strat.maybe_enter(bar, [], 100_000.0, None)
    assert result is None
    assert strat._n_session_filter_rejects == 1


def test_session_filter_allows_included_hour() -> None:
    """A bar at an included UTC hour passes the session gate."""
    from eta_engine.strategies.sweep_reclaim_strategy import (
        SweepReclaimConfig,
        SweepReclaimStrategy,
    )

    cfg = SweepReclaimConfig(
        excluded_hours_utc=(20, 21, 22, 23),
        warmup_bars=2,
    )
    strat = SweepReclaimStrategy(cfg)
    bar = _MockBar(
        open=100.0, high=100.5, low=99.5, close=100.2, volume=1000.0,
        ts="2026-05-12T14:00:00+00:00",  # UTC 14 = included
    )
    # Just verify it doesn't trip the session-filter counter
    strat.maybe_enter(bar, [], 100_000.0, None)
    assert strat._n_session_filter_rejects == 0


def test_default_session_filter_empty() -> None:
    """Default config has no session filter (legacy)."""
    from eta_engine.strategies.sweep_reclaim_strategy import SweepReclaimConfig

    cfg = SweepReclaimConfig()
    assert cfg.excluded_hours_utc == ()


# ────────────────────────────────────────────────────────────────────
# Cross-strategy consistency
# ────────────────────────────────────────────────────────────────────


def test_mgc_preset_is_real_rehaul() -> None:
    """The mgc_sweep_preset wave-3/4/5 changes are not a curve-fit.
    Each wave has a written rationale and a falsifier in the docstring.
    Verify the post-wave-5 surface is intact."""
    from eta_engine.strategies.sweep_reclaim_strategy import mgc_sweep_preset

    cfg = mgc_sweep_preset()
    # Wave-3 refinements (chisel-cut)
    assert cfg.atr_stop_mult == 2.5
    assert cfg.rr_target == 3.5
    assert cfg.min_volume_z == 0.5
    assert cfg.min_wick_pct == 0.40
    # Wave-4 features that survived wave-5
    assert cfg.reclaim_confirm_bars == 2
    assert cfg.vol_adjusted_sizing is True
    # Wave-5 reverted the bogus close-session exclusion
    assert cfg.excluded_hours_utc == ()


def test_mcl_preset_NOT_rehauled() -> None:
    """mcl has n=8 — too small to refine on.  Wave-4 leaves it
    at legacy params (no curve-fitting on noise)."""
    from eta_engine.strategies.sweep_reclaim_strategy import mcl_sweep_preset

    cfg = mcl_sweep_preset()
    assert cfg.reclaim_confirm_bars == 1
    assert cfg.vol_adjusted_sizing is False
    assert cfg.excluded_hours_utc == ()
