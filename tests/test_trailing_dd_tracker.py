"""
EVOLUTIONARY TRADING ALGO  //  tests.test_trailing_dd_tracker
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

from eta_engine.core.kill_switch_runtime import ApexEvalSnapshot
from eta_engine.core.trailing_dd_tracker import (
    ResetNotAcknowledgedError,
    TrailingDDAuditLog,
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
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        s = fresh_tracker.state()
        assert s.starting_balance_usd == APEX_50K_START
        assert s.trailing_dd_cap_usd == APEX_50K_CAP
        assert s.peak_equity_usd == APEX_50K_START
        assert s.frozen is False
        assert s.last_equity_usd is None
        assert s.breach_count == 0

    def test_fresh_init_writes_file_to_disk(
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        assert fresh_tracker.path.exists()
        raw = json.loads(fresh_tracker.path.read_text())
        assert raw["starting_balance_usd"] == APEX_50K_START
        assert raw["peak_equity_usd"] == APEX_50K_START
        assert raw["frozen"] is False

    def test_load_roundtrip_preserves_state(
        self,
        tracker_path: Path,
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
        self,
        tracker_path: Path,
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
        self,
        tracker_path: Path,
    ) -> None:
        tracker_path.write_text("{not-json", encoding="utf-8")
        with pytest.raises(TrailingDDCorruptError, match="corrupt"):
            TrailingDDTracker.load_or_init(
                path=tracker_path,
                starting_balance_usd=APEX_50K_START,
                trailing_dd_cap_usd=APEX_50K_CAP,
            )

    def test_baseline_mismatch_on_load_raises(
        self,
        tracker_path: Path,
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
        self,
        tracker_path: Path,
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
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=50_500.0)
        assert fresh_tracker.state().peak_equity_usd == 50_500.0
        fresh_tracker.update(current_equity_usd=51_200.0)
        assert fresh_tracker.state().peak_equity_usd == 51_200.0

    def test_lower_equity_does_not_lower_peak(
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_500.0)
        fresh_tracker.update(current_equity_usd=49_800.0)  # below start
        assert fresh_tracker.state().peak_equity_usd == 51_500.0

    def test_lower_equity_updates_last_mark(
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_500.0)
        fresh_tracker.update(current_equity_usd=49_800.0)
        assert fresh_tracker.state().last_equity_usd == 49_800.0


# ---------------------------------------------------------------------------
# Floor formula + freeze rule
# ---------------------------------------------------------------------------


class TestFloorAndFreeze:
    def test_floor_before_freeze(
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        # peak=50_000 -> floor = 50_000 - 2_500 = 47_500
        assert fresh_tracker.floor_usd() == pytest.approx(47_500.0)

        fresh_tracker.update(current_equity_usd=51_500.0)
        # peak=51_500 -> floor = 51_500 - 2_500 = 49_000
        assert fresh_tracker.floor_usd() == pytest.approx(49_000.0)

    def test_freeze_triggers_exactly_at_threshold(
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=52_499.99)
        assert fresh_tracker.state().frozen is False

        fresh_tracker.update(current_equity_usd=APEX_50K_FREEZE)
        assert fresh_tracker.state().frozen is True

    def test_floor_locks_at_starting_balance_after_freeze(
        self,
        fresh_tracker: TrailingDDTracker,
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
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=52_600.0)
        assert fresh_tracker.state().frozen is True
        pre = fresh_tracker.state().peak_equity_usd
        fresh_tracker.update(current_equity_usd=55_000.0)
        # Peak is frozen in the sense that it does not update the
        # floor; we preserve pre-freeze peak for forensic clarity.
        assert fresh_tracker.state().peak_equity_usd == pytest.approx(pre)

    def test_freeze_survives_restart(
        self,
        tracker_path: Path,
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
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        snap = fresh_tracker.snapshot()
        assert isinstance(snap, ApexEvalSnapshot)

    def test_snapshot_distance_with_no_update_uses_peak(
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        # No update() yet. mark=peak=starting, floor=starting-cap.
        # distance = starting - (starting - cap) = cap.
        snap = fresh_tracker.snapshot()
        assert snap.distance_to_limit_usd == pytest.approx(APEX_50K_CAP)

    def test_distance_reflects_last_tick(
        self,
        fresh_tracker: TrailingDDTracker,
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
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_000.0)
        # At floor: equity=48_500, dist=0
        snap = fresh_tracker.update(current_equity_usd=48_500.0)
        assert snap.distance_to_limit_usd == pytest.approx(0.0)

    def test_distance_clipped_to_zero_below_floor(
        self,
        fresh_tracker: TrailingDDTracker,
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
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_000.0)
        fresh_tracker.update(current_equity_usd=49_000.0)  # above 48_500 floor
        assert fresh_tracker.state().breach_count == 0

    def test_breach_at_floor(
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        # peak seeded at start=50_000, floor=47_500
        fresh_tracker.update(current_equity_usd=47_500.0)
        assert fresh_tracker.state().breach_count == 1

    def test_breach_below_floor(
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=47_000.0)
        assert fresh_tracker.state().breach_count == 1

    def test_breach_counts_each_tick(
        self,
        fresh_tracker: TrailingDDTracker,
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
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_000.0)
        fresh_tracker.update(current_equity_usd=52_800.0)
        assert fresh_tracker.state().frozen is True
        assert fresh_tracker.state().peak_equity_usd == 52_800.0

        fresh_tracker.reset(
            starting_balance_usd=APEX_50K_START,
            operator="test",
            acknowledge_destruction=True,
            reason="test fixture reset",
        )
        s = fresh_tracker.state()
        assert s.peak_equity_usd == APEX_50K_START
        assert s.frozen is False
        assert s.last_equity_usd is None
        assert s.breach_count == 0

    def test_reset_rejects_non_positive_balance(
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        with pytest.raises(ValueError, match="starting_balance_usd"):
            fresh_tracker.reset(
                starting_balance_usd=0.0,
                operator="test",
                acknowledge_destruction=True,
            )

    def test_reset_persists(
        self,
        fresh_tracker: TrailingDDTracker,
        tracker_path: Path,
    ) -> None:
        fresh_tracker.update(current_equity_usd=52_800.0)
        fresh_tracker.reset(
            starting_balance_usd=APEX_50K_START,
            operator="test",
            acknowledge_destruction=True,
        )

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
        self,
        fresh_tracker: TrailingDDTracker,
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
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        state = fresh_tracker.state()
        assert isinstance(state, TrailingDDState)


# ---------------------------------------------------------------------------
# R3 closure: audit log + reset acknowledgment
# ---------------------------------------------------------------------------
# These tests cover the red-team R3 finding: tracker state changes were
# silent, so a rogue reset or a process re-init could erase the frozen
# floor invariant with no forensic trail. Audit log is append-only; reset
# requires explicit operator attribution + destruction acknowledgment.


class TestAuditLogInitAndLoad:
    def test_fresh_init_writes_init_event(
        self,
        tracker_path: Path,
    ) -> None:
        t = TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        audit = TrailingDDAuditLog(
            path=tracker_path.parent / (tracker_path.name + ".audit.jsonl"),
        )
        events = audit.read_all()
        assert len(events) == 1
        assert events[0]["event"] == "init"
        assert events[0]["seq"] == 1
        assert events[0]["state"]["starting_balance_usd"] == APEX_50K_START
        # state file and audit log co-exist
        assert t.path.exists()

    def test_existing_file_emits_load_event(
        self,
        tracker_path: Path,
    ) -> None:
        # First init -> init event
        TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        # Second load from same file -> load event
        TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        audit = TrailingDDAuditLog(
            path=tracker_path.parent / (tracker_path.name + ".audit.jsonl"),
        )
        events = audit.read_all()
        assert [e["event"] for e in events] == ["init", "load"]
        assert [e["seq"] for e in events] == [1, 2]

    def test_audit_file_colocated_with_state_by_default(
        self,
        tracker_path: Path,
    ) -> None:
        TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        expected = tracker_path.parent / (tracker_path.name + ".audit.jsonl")
        assert expected.exists()
        # Contains a parseable JSON line per event.
        first_line = expected.read_text(encoding="utf-8").splitlines()[0]
        assert json.loads(first_line)["event"] == "init"

    def test_explicit_audit_log_path(
        self,
        tmp_path: Path,
        tracker_path: Path,
    ) -> None:
        custom = tmp_path / "custom_dir" / "apex_audit.jsonl"
        TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
            audit_log_path=custom,
        )
        assert custom.exists()
        rec = json.loads(custom.read_text().splitlines()[0])
        assert rec["event"] == "init"


class TestAuditLogFreezeAndBreach:
    def test_freeze_event_emitted_once_at_transition(
        self,
        fresh_tracker: TrailingDDTracker,
        tracker_path: Path,
    ) -> None:
        fresh_tracker.update(current_equity_usd=52_600.0)  # crosses freeze
        fresh_tracker.update(current_equity_usd=55_000.0)  # already frozen
        fresh_tracker.update(current_equity_usd=54_500.0)  # already frozen
        audit = TrailingDDAuditLog(
            path=tracker_path.parent / (tracker_path.name + ".audit.jsonl"),
        )
        freeze_events = [e for e in audit.read_all() if e["event"] == "freeze"]
        assert len(freeze_events) == 1
        assert freeze_events[0]["state"]["frozen"] is True
        assert freeze_events[0]["locked_floor_usd"] == APEX_50K_START
        assert freeze_events[0]["freeze_threshold_usd"] == APEX_50K_FREEZE

    def test_breach_event_emitted_per_tick_below_floor(
        self,
        fresh_tracker: TrailingDDTracker,
        tracker_path: Path,
    ) -> None:
        # peak starts at 50_000 -> floor = 47_500
        fresh_tracker.update(current_equity_usd=47_000.0)
        fresh_tracker.update(current_equity_usd=46_000.0)
        fresh_tracker.update(current_equity_usd=47_200.0)  # still below 47_500
        audit = TrailingDDAuditLog(
            path=tracker_path.parent / (tracker_path.name + ".audit.jsonl"),
        )
        breach_events = [e for e in audit.read_all() if e["event"] == "breach"]
        assert len(breach_events) == 3
        # Each event records the equity at the time of breach
        assert breach_events[0]["equity_usd"] == 47_000.0
        assert breach_events[1]["equity_usd"] == 46_000.0
        assert breach_events[2]["equity_usd"] == 47_200.0
        # Floor is unchanged (peak didn't advance)
        for e in breach_events:
            assert e["floor_usd"] == pytest.approx(47_500.0)

    def test_no_breach_event_above_floor(
        self,
        fresh_tracker: TrailingDDTracker,
        tracker_path: Path,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_500.0)
        fresh_tracker.update(current_equity_usd=49_500.0)
        audit = TrailingDDAuditLog(
            path=tracker_path.parent / (tracker_path.name + ".audit.jsonl"),
        )
        assert not any(e["event"] == "breach" for e in audit.read_all())


class TestAuditLogSequenceMonotonicity:
    def test_sequence_monotonically_increases_across_events(
        self,
        fresh_tracker: TrailingDDTracker,
        tracker_path: Path,
    ) -> None:
        fresh_tracker.update(current_equity_usd=51_000.0)  # no audit event
        fresh_tracker.update(current_equity_usd=52_700.0)  # freeze
        fresh_tracker.update(current_equity_usd=47_000.0)  # breach
        fresh_tracker.update(current_equity_usd=46_500.0)  # breach
        audit = TrailingDDAuditLog(
            path=tracker_path.parent / (tracker_path.name + ".audit.jsonl"),
        )
        events = audit.read_all()
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs)
        assert seqs == list(range(1, len(events) + 1))
        # Events: init, freeze, breach, breach
        assert [e["event"] for e in events] == [
            "init",
            "freeze",
            "breach",
            "breach",
        ]


class TestResetAcknowledgment:
    def test_reset_without_ack_raises(
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        with pytest.raises(ResetNotAcknowledgedError, match="destructive"):
            fresh_tracker.reset(
                starting_balance_usd=APEX_50K_START,
                operator="rogue_script",
                # acknowledge_destruction defaults to False
            )

    def test_reset_ack_false_explicit_raises(
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        with pytest.raises(ResetNotAcknowledgedError):
            fresh_tracker.reset(
                starting_balance_usd=APEX_50K_START,
                operator="rogue_script",
                acknowledge_destruction=False,
            )

    def test_reset_without_operator_raises(
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        with pytest.raises(ValueError, match="operator"):
            fresh_tracker.reset(
                starting_balance_usd=APEX_50K_START,
                operator="",
                acknowledge_destruction=True,
            )

    def test_reset_with_whitespace_operator_raises(
        self,
        fresh_tracker: TrailingDDTracker,
    ) -> None:
        with pytest.raises(ValueError, match="operator"):
            fresh_tracker.reset(
                starting_balance_usd=APEX_50K_START,
                operator="   ",
                acknowledge_destruction=True,
            )

    def test_reset_emits_audit_event_with_operator_and_reason(
        self,
        fresh_tracker: TrailingDDTracker,
        tracker_path: Path,
    ) -> None:
        fresh_tracker.update(current_equity_usd=52_700.0)  # causes freeze
        fresh_tracker.reset(
            starting_balance_usd=APEX_50K_START,
            operator="edward",
            acknowledge_destruction=True,
            reason="fresh 50K eval after prior bust",
        )
        audit = TrailingDDAuditLog(
            path=tracker_path.parent / (tracker_path.name + ".audit.jsonl"),
        )
        reset_events = [e for e in audit.read_all() if e["event"] == "reset"]
        assert len(reset_events) == 1
        ev = reset_events[0]
        assert ev["operator"] == "edward"
        assert ev["reason"] == "fresh 50K eval after prior bust"
        # prior_state captured for forensics
        assert ev["prior_state"]["frozen"] is True
        assert ev["prior_state"]["peak_equity_usd"] == 52_700.0
        # new state is fresh
        assert ev["state"]["frozen"] is False
        assert ev["state"]["peak_equity_usd"] == APEX_50K_START

    def test_reset_does_not_clear_audit_log(
        self,
        fresh_tracker: TrailingDDTracker,
        tracker_path: Path,
    ) -> None:
        fresh_tracker.update(current_equity_usd=52_700.0)
        fresh_tracker.reset(
            starting_balance_usd=APEX_50K_START,
            operator="edward",
            acknowledge_destruction=True,
        )
        audit = TrailingDDAuditLog(
            path=tracker_path.parent / (tracker_path.name + ".audit.jsonl"),
        )
        events = audit.read_all()
        # init + freeze + reset -> 3 events
        assert [e["event"] for e in events] == ["init", "freeze", "reset"]


class TestAuditLogSurvivesStateDeletion:
    def test_audit_log_preserved_if_state_file_deleted(
        self,
        fresh_tracker: TrailingDDTracker,
        tracker_path: Path,
    ) -> None:
        """R3 scenario: an attacker (or a well-meaning 'cleanup' script)
        deletes the state file to erase the frozen floor. The audit log
        still shows the freeze + breach trail, so a forensic reviewer
        can detect the tampering."""
        fresh_tracker.update(current_equity_usd=52_800.0)  # freeze
        audit_path = tracker_path.parent / (tracker_path.name + ".audit.jsonl")
        pre_events = TrailingDDAuditLog(audit_path).read_all()

        # Delete only the state file
        tracker_path.unlink()
        assert not tracker_path.exists()
        assert audit_path.exists()

        # Re-init -> this creates a fresh state but ALSO logs a new init
        # event, so the audit log shows state_file_vanished_then_reinit.
        TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        post_events = TrailingDDAuditLog(audit_path).read_all()
        assert len(post_events) == len(pre_events) + 1
        # The prior freeze is still visible in the audit log.
        assert any(e["event"] == "freeze" for e in post_events)
        # The post-deletion re-init is recorded as a new init.
        assert post_events[-1]["event"] == "init"


class TestTrailingDDAuditLogUnit:
    """Direct unit tests on the TrailingDDAuditLog class."""

    def test_read_all_returns_empty_list_when_file_missing(
        self,
        tmp_path: Path,
    ) -> None:
        log = TrailingDDAuditLog(tmp_path / "nonexistent.jsonl")
        assert log.read_all() == []

    def test_append_creates_parent_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "audit.jsonl"
        log = TrailingDDAuditLog(target)
        log.append("init", {"starting_balance_usd": 50_000.0})
        assert target.exists()
        lines = target.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["event"] == "init"
        assert rec["seq"] == 1

    def test_append_preserves_prior_events(self, tmp_path: Path) -> None:
        target = tmp_path / "audit.jsonl"
        log = TrailingDDAuditLog(target)
        log.append("init", {"a": 1})
        log.append("freeze", {"a": 2})
        log.append("breach", {"a": 3})
        events = log.read_all()
        assert [e["event"] for e in events] == ["init", "freeze", "breach"]
        assert [e["seq"] for e in events] == [1, 2, 3]

    def test_append_skips_corrupt_lines_on_read(
        self,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "audit.jsonl"
        log = TrailingDDAuditLog(target)
        log.append("init", {"a": 1})
        # Inject a corrupt line between good ones
        with target.open("a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
        log.append("freeze", {"a": 2})
        events = log.read_all()
        # Corrupt line skipped; both good lines remain.
        assert [e["event"] for e in events] == ["init", "freeze"]


# ---------------------------------------------------------------------------
# Chaos drill: audit-log fsync failure behaviour
# ---------------------------------------------------------------------------


class TestAuditLogFsyncChaos:
    """Chaos drill: what happens when os.fsync raises during audit append?

    Scenario that motivated this: v0.1.59 added fsync-per-append for R3
    durability. In production the audit log lives under ``state/`` which
    may sit on a OneDrive-synced or network-shared volume where fsync
    is known to fail or no-op under load / reparse-point conditions.
    The buffered write has already succeeded at the OS level by the
    time fsync fires, so the append is NOT lost -- but durability is.

    The chaos drill pins down the exact behaviour:
      * tracker keeps enforcing the floor (runtime continuity)
      * the audit record is visible to read_all() (write persisted)
      * fsync_failure_count increments (health-probe signal)
      * last_fsync_error populated with exception text
      * a WARNING log is emitted (operator visibility)

    v0.1.59 originally swallowed at DEBUG level. v0.1.60 upgraded to
    WARNING + counter so a read-only / reparse-point volume doesn't
    silently degrade durability.
    """

    def test_fresh_log_has_zero_fsync_failures(self, tmp_path: Path) -> None:
        log = TrailingDDAuditLog(tmp_path / "audit.jsonl")
        assert log.fsync_failure_count == 0
        assert log.last_fsync_error == ""

    def test_fsync_failure_increments_counter_and_records_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = TrailingDDAuditLog(tmp_path / "audit.jsonl")

        def _boom(_fd: int) -> None:
            raise OSError("EROFS: read-only filesystem")

        import os as _os

        monkeypatch.setattr(_os, "fsync", _boom)
        log.append("init", {"x": 1})

        assert log.fsync_failure_count == 1
        assert "OSError" in log.last_fsync_error
        assert "EROFS" in log.last_fsync_error

    def test_fsync_failure_does_not_lose_the_written_record(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / "audit.jsonl"
        log = TrailingDDAuditLog(target)

        import os as _os

        monkeypatch.setattr(_os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("sim")))
        log.append("freeze", {"peak": 52_500.0})

        # The buffered write landed on disk even though fsync failed.
        events = log.read_all()
        assert len(events) == 1
        assert events[0]["event"] == "freeze"
        assert events[0]["state"]["peak"] == 52_500.0

    def test_fsync_failures_accumulate_across_appends(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = TrailingDDAuditLog(tmp_path / "audit.jsonl")

        import os as _os

        monkeypatch.setattr(_os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("sim")))
        log.append("init", {"a": 1})
        log.append("freeze", {"a": 2})
        log.append("breach", {"a": 3})

        assert log.fsync_failure_count == 3
        # All three records landed.
        events = log.read_all()
        assert [e["event"] for e in events] == ["init", "freeze", "breach"]

    def test_fsync_recovery_does_not_reset_counter(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = TrailingDDAuditLog(tmp_path / "audit.jsonl")

        import os as _os

        # First two appends: fsync fails.
        monkeypatch.setattr(_os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("sim")))
        log.append("init", {"a": 1})
        log.append("freeze", {"a": 2})
        assert log.fsync_failure_count == 2

        # Recovery: fsync starts working again. Counter must persist --
        # the operator needs to know that durability WAS degraded, even
        # if the volume is healthy again now.
        monkeypatch.setattr(_os, "fsync", lambda _fd: None)
        log.append("breach", {"a": 3})
        assert log.fsync_failure_count == 2
        assert "OSError" in log.last_fsync_error  # NOT cleared on recovery

    def test_fsync_failure_emits_warning_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        log = TrailingDDAuditLog(tmp_path / "audit.jsonl")

        import logging as _logging
        import os as _os

        monkeypatch.setattr(_os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("sim")))
        caplog.set_level(_logging.WARNING, logger="eta_engine.core.trailing_dd_tracker")

        log.append("freeze", {"peak": 52_500.0})

        # At least one WARNING record should mention audit fsync.
        warnings = [r for r in caplog.records if r.levelno >= _logging.WARNING]
        assert any("audit fsync failed" in r.getMessage() for r in warnings)

    def test_tracker_keeps_enforcing_floor_when_audit_fsync_fails(
        self,
        tracker_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The full chaos-drill scenario: during a freeze event, the
        audit log fsync wedges (e.g. OneDrive reparse-point transient).
        The tracker must keep enforcing the floor -- the state file
        write is a separate path and should not be affected by the
        audit-fsync failure.
        """
        import os as _os

        monkeypatch.setattr(_os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("sim")))

        tracker = TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        # Push equity above the cap to trigger freeze.
        tracker.update(current_equity_usd=APEX_50K_FREEZE + 100.0)
        s = tracker.state()
        assert s.frozen is True
        assert s.peak_equity_usd >= APEX_50K_FREEZE

        # Floor now locked. Drive equity below the floor -> breach.
        tracker.update(current_equity_usd=APEX_50K_START - 50.0)
        s = tracker.state()
        assert s.breach_count >= 1

        # Audit log registered every failure -- init + load or init alone
        # on fresh path + freeze + breach => at least 3 fsync calls,
        # all failed.
        assert tracker._audit.fsync_failure_count >= 3
        # But the file was still written, so forensic review works.
        events = tracker._audit.read_all()
        assert any(e["event"] == "freeze" for e in events)
        assert any(e["event"] == "breach" for e in events)

    def test_tracker_state_file_write_independent_of_audit_fsync(
        self,
        tracker_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The tracker's own state file write has its own fsync path
        (in _atomic_write). Monkeypatching os.fsync fails BOTH the
        audit-log fsync AND the state-file fsync, so this test
        verifies the tracker continues even when both paths fail.

        v0.1.59's state-file write also wraps fsync in a try/except
        (line 347-349). This test pins that combined chaos behaviour.
        """
        import os as _os

        monkeypatch.setattr(_os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("sim")))

        tracker = TrailingDDTracker.load_or_init(
            path=tracker_path,
            starting_balance_usd=APEX_50K_START,
            trailing_dd_cap_usd=APEX_50K_CAP,
        )
        tracker.update(current_equity_usd=APEX_50K_START + 500.0)

        # State file exists and is parseable despite fsync failures.
        assert tracker_path.exists()
        data = json.loads(tracker_path.read_text(encoding="utf-8"))
        assert data["starting_balance_usd"] == APEX_50K_START
        assert data["peak_equity_usd"] >= APEX_50K_START + 500.0
