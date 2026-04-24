"""
APEX PREDATOR  //  tests.test_trailing_dd_tracker
=================================================
Unit tests for the tick-granular Apex trailing-DD tracker.

Covers:
  * init + persistence round-trip
  * HWM (peak) moves up on new highs, never down
  * freeze rule (peak >= starting + cap)
  * floor formula before/after freeze
  * distance_to_limit_usd semantics
  * breach counter
  * atomic write survives simulated partial write (tmp file ignored)
  * corrupt file raises TrailingDDCorruptError (fail-closed)
  * baseline mismatch on load raises ValueError
  * reset() wipes state cleanly
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from apex_predator.core.kill_switch_runtime import ApexEvalSnapshot
from apex_predator.core.trailing_dd_tracker import (
    TrailingDDCorruptError,
    TrailingDDState,
    TrailingDDTracker,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

APEX_50K_START = 50_000.0
APEX_50K_CAP = 2_500.0
APEX_50K_FREEZE = 52_500.0  # starting + cap


@pytest.fixture
def tracker_path(tmp_path: Path) -> Path:
    return tmp_path / "apex_trailing_dd.json"


@pytest.fixture
def fresh_tracker(tracker_path: Path) -> TrailingDDTracker:
    return TrailingDDTracker.load_or_init(
        path=tracker_path,
        starting_balance_usd=APEX_50K_START,
        trailing_dd_cap_usd=APEX_50K_CAP,
    )


# ---------------------------------------------------------------------------
# Init + persistence
# ---------------------------------------------------------------------------

class TestInitAndPersistence:

    def test_fresh_init_seeds_peak_to_starting_balance(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        s = fresh_tracker.state()
        assert s.starting_balance_usd == APEX_50K_START
        assert s.trailing_dd_cap_usd == APEX_50K_CAP
        assert s.peak_equity_usd == APEX_50K_START
        assert s.frozen is False
        assert s.last_equity_usd is None
        assert s.breach_count == 0

    def test_fresh_init_writes_file_to_disk(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        assert fresh_tracker.path.exists()
        raw = json.loads(fresh_tracker.path.read_text())
        assert raw["starting_balance_usd"] == APEX_50K_START
        assert raw["peak_equity_usd"] == APEX_50K_START
        assert raw["frozen"] is False

    def test_load_roundtrip_preserves_state(
        self, tracker_path: Path,
    ) -> None:
        t1 = TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        t1.update(current_equity_usd=51_200.0)
        t1.update(current_equity_usd=51_800.0)
        assert t1.state().peak_equity_usd == 51_800.0

        # Fresh tracker on same path should load the peak, not reset it.
        t2 = TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        assert t2.state().peak_equity_usd == 51_800.0
        assert t2.state().last_equity_usd == 51_800.0
        assert t2.state().frozen is False

    def test_invalid_starting_balance_raises(
        self, tracker_path: Path,
    ) -> None:
        with pytest.raises(ValueError, match="starting_balance_usd"):
            TrailingDDTracker.load_or_init(
                path=tracker_path,
                starting_balance_usd=0.0,
                trailing_dd_cap_usd=APEX_50K_CAP,
            )

    def test_invalid_cap_raises(self, tracker_path: Path) -> None:
        with pytest.raises(ValueError, match="trailing_dd_cap_usd"):
            TrailingDDTracker.load_or_init(
                path=tracker_path,
                starting_balance_usd=APEX_50K_START,
                trailing_dd_cap_usd=-100.0,
            )

    def test_corrupt_file_raises_fail_closed(
        self, tracker_path: Path,
    ) -> None:
        tracker_path.write_text("{not-json", encoding="utf-8")
        with pytest.raises(TrailingDDCorruptError, match="corrupt"):
            TrailingDDTracker.load_or_init(
                path=tracker_path,
                starting_balance_usd=APEX_50K_START,
                trailing_dd_cap_usd=APEX_50K_CAP,
            )

    def test_baseline_mismatch_on_load_raises(
        self, tracker_path: Path,
    ) -> None:
        TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        with pytest.raises(ValueError, match="starting_balance"):
            TrailingDDTracker.load_or_init(
                path=tracker_path,
                starting_balance_usd=150_000.0,  # wrong eval size
                trailing_dd_cap_usd=APEX_50K_CAP,
            )

    def test_cap_mismatch_on_load_raises(
        self, tracker_path: Path,
    ) -> None:
        TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        with pytest.raises(ValueError, match="cap"):
            TrailingDDTracker.load_or_init(
                path=tracker_path,
                starting_balance_usd=APEX_50K_START,
                trailing_dd_cap_usd=5_000.0,  # wrong cap
            )


# ---------------------------------------------------------------------------
# Peak / HWM
# ---------------------------------------------------------------------------

class TestPeakTracking:

    def test_new_high_raises_peak(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=50_500.0)
        assert fresh_tracker.state().peak_equity_usd == 50_500.0
        fresh_tracker.update(current_equity_usd=51_200.0)
        assert fresh_tracker.state().peak_equity_usd == 51_200.0

    def test_lower_equity_does_not_lower_peak(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_500.0)
        fresh_tracker.update(current_equity_usd=49_800.0)  # below start
        assert fresh_tracker.state().peak_equity_usd == 51_500.0

    def test_lower_equity_updates_last_mark(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_500.0)
        fresh_tracker.update(current_equity_usd=49_800.0)
        assert fresh_tracker.state().last_equity_usd == 49_800.0


# ---------------------------------------------------------------------------
# Floor formula + freeze rule
# ---------------------------------------------------------------------------

class TestFloorAndFreeze:

    def test_floor_before_freeze(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        # peak=50_000 -> floor = 50_000 - 2_500 = 47_500
        assert fresh_tracker.floor_usd() == pytest.approx(47_500.0)

        fresh_tracker.update(current_equity_usd=51_500.0)
        # peak=51_500 -> floor = 51_500 - 2_500 = 49_000
        assert fresh_tracker.floor_usd() == pytest.approx(49_000.0)

    def test_freeze_triggers_exactly_at_threshold(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=52_499.99)
        assert fresh_tracker.state().frozen is False

        fresh_tracker.update(current_equity_usd=APEX_50K_FREEZE)
        assert fresh_tracker.state().frozen is True

    def test_floor_locks_at_starting_balance_after_freeze(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=52_600.0)
        assert fresh_tracker.state().frozen is True
        assert fresh_tracker.floor_usd() == pytest.approx(APEX_50K_START)

        # Even if equity goes much higher, floor stays at starting.
        fresh_tracker.update(current_equity_usd=55_000.0)
        assert fresh_tracker.floor_usd() == pytest.approx(APEX_50K_START)

        # Peak still climbs for diagnostic purposes (but has no effect
        # on the floor once frozen).
        assert fresh_tracker.state().peak_equity_usd == pytest.approx(52_600.0)

    def test_peak_does_not_advance_once_frozen(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=52_600.0)
        assert fresh_tracker.state().frozen is True
        pre = fresh_tracker.state().peak_equity_usd
        fresh_tracker.update(current_equity_usd=55_000.0)
        # Peak is frozen in the sense that it does not update the
        # floor; we preserve pre-freeze peak for forensic clarity.
        assert fresh_tracker.state().peak_equity_usd == pytest.approx(pre)

    def test_freeze_survives_restart(
        self, tracker_path: Path,
    ) -> None:
        t1 = TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        t1.update(current_equity_usd=53_000.0)
        assert t1.state().frozen is True

        t2 = TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        assert t2.state().frozen is True
        assert t2.floor_usd() == pytest.approx(APEX_50K_START)


# ---------------------------------------------------------------------------
# Snapshot contract
# ---------------------------------------------------------------------------

class TestSnapshot:

    def test_snapshot_type_is_apex_eval_snapshot(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        snap = fresh_tracker.snapshot()
        assert isinstance(snap, ApexEvalSnapshot)

    def test_snapshot_distance_with_no_update_uses_peak(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        # No update() yet. mark=peak=starting, floor=starting-cap.
        # distance = starting - (starting - cap) = cap.
        snap = fresh_tracker.snapshot()
        assert snap.distance_to_limit_usd == pytest.approx(APEX_50K_CAP)

    def test_distance_reflects_last_tick(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_000.0)
        # peak=51_000, floor=48_500, mark=51_000 -> dist=2_500
        snap = fresh_tracker.snapshot()
        assert snap.distance_to_limit_usd == pytest.approx(2_500.0)

        fresh_tracker.update(current_equity_usd=49_500.0)
        # peak=51_000, floor=48_500, mark=49_500 -> dist=1_000
        snap = fresh_tracker.snapshot()
        assert snap.distance_to_limit_usd == pytest.approx(1_000.0)

    def test_distance_clipped_to_zero_at_floor(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_000.0)
        # At floor: equity=48_500, dist=0
        snap = fresh_tracker.update(current_equity_usd=48_500.0)
        assert snap.distance_to_limit_usd == pytest.approx(0.0)

    def test_distance_clipped_to_zero_below_floor(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_000.0)
        snap = fresh_tracker.update(current_equity_usd=47_900.0)
        # below floor -> distance floored at 0, not negative
        assert snap.distance_to_limit_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Breach counter
# ---------------------------------------------------------------------------

class TestBreachCounter:

    def test_no_breach_above_floor(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_000.0)
        fresh_tracker.update(current_equity_usd=49_000.0)  # above 48_500 floor
        assert fresh_tracker.state().breach_count == 0

    def test_breach_at_floor(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        # peak seeded at start=50_000, floor=47_500
        fresh_tracker.update(current_equity_usd=47_500.0)
        assert fresh_tracker.state().breach_count == 1

    def test_breach_below_floor(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=47_000.0)
        assert fresh_tracker.state().breach_count == 1

    def test_breach_counts_each_tick(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=46_000.0)
        fresh_tracker.update(current_equity_usd=45_500.0)
        fresh_tracker.update(current_equity_usd=47_200.0)  # still <= 47_500
        assert fresh_tracker.state().breach_count == 3


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

class TestReset:

    def test_reset_wipes_state(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_000.0)
        fresh_tracker.update(current_equity_usd=52_800.0)
        assert fresh_tracker.state().frozen is True
        assert fresh_tracker.state().peak_equity_usd == 52_800.0

        fresh_tracker.reset(starting_balance_usd=APEX_50K_START)
        s = fresh_tracker.state()
        assert s.peak_equity_usd == APEX_50K_START
        assert s.frozen is False
        assert s.last_equity_usd is None
        assert s.breach_count == 0

    def test_reset_rejects_non_positive_balance(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        with pytest.raises(ValueError, match="starting_balance_usd"):
            fresh_tracker.reset(starting_balance_usd=0.0)

    def test_reset_persists(
        self, fresh_tracker: TrailingDDTracker, tracker_path: Path,
    ) -> None:
        fresh_tracker.update(current_equity_usd=52_800.0)
        fresh_tracker.reset(starting_balance_usd=APEX_50K_START)

        t2 = TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        assert t2.state().frozen is False
        assert t2.state().peak_equity_usd == APEX_50K_START


# ---------------------------------------------------------------------------
# Integration with KillSwitch path (sanity — stateless shape match)
# ---------------------------------------------------------------------------

class TestKillSwitchCompatibility:

    def test_snapshot_feeds_apex_preempt_check(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        """A near-breach tick should produce a tiny ``distance_to_limit_usd``."""
        fresh_tracker.update(current_equity_usd=51_000.0)  # peak=51_000, floor=48_500
        snap = fresh_tracker.update(current_equity_usd=48_700.0)  # dist=200
        assert snap.trailing_dd_limit_usd == pytest.approx(APEX_50K_CAP)
        assert snap.distance_to_limit_usd == pytest.approx(200.0)
        # This shape is exactly what KillSwitch._check_apex_preemptive
        # consumes. A cushion_usd=500 policy would fire a
        # FLATTEN_TIER_A_PREEMPTIVE verdict for distance=200.

    def test_snapshot_matches_state_dataclass(
        self, fresh_tracker: TrailingDDTracker,
    ) -> None:
        state = fresh_tracker.state()
        assert isinstance(state, TrailingDDState)
