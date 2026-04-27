"""Tests for core.kill_switch_latch -- the persistent boot-gate latch.

Covers:
  * first-ever boot on a clean disk => ARMED (boot allowed)
  * FLATTEN_ALL verdict trips the latch
  * TRIPPED latch refuses boot
  * second TRIPPING verdict does NOT overwrite the original (first-trip-wins)
  * non-latching verdicts (HALVE_SIZE, CONTINUE, FLATTEN_BOT) do NOT trip
  * clear() resets to ARMED and preserves audit trail
  * clear() requires a non-empty operator name
  * corrupt latch JSON => fail-closed (treated as TRIPPED)
  * atomic write leaves no .tmp file behind
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from eta_engine.core.kill_switch_latch import (
    KillSwitchLatch,
    LatchState,
)
from eta_engine.core.kill_switch_runtime import (
    KillAction,
    KillSeverity,
    KillVerdict,
)

if TYPE_CHECKING:
    from pathlib import Path


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _flatten_all_verdict() -> KillVerdict:
    return KillVerdict(
        action=KillAction.FLATTEN_ALL,
        severity=KillSeverity.CRITICAL,
        reason="daily loss 6.02% >= cap 6%",
        scope="global",
        evidence={"daily_loss_pct": 6.02, "cap_pct": 6.0},
    )


def _preemptive_verdict() -> KillVerdict:
    return KillVerdict(
        action=KillAction.FLATTEN_TIER_A_PREEMPTIVE,
        severity=KillSeverity.CRITICAL,
        reason="apex cushion 200 <= preempt 500",
        scope="tier_a",
        evidence={"distance_to_limit_usd": 200, "cushion_usd": 500},
    )


def _flatten_bot_verdict() -> KillVerdict:
    return KillVerdict(
        action=KillAction.FLATTEN_BOT,
        severity=KillSeverity.WARN,
        reason="mnq session pnl -600 <= -$500 trip-wire",
        scope="bot:mnq",
        evidence={"session_realized_pnl_usd": -600, "max_loss_usd": 500},
    )


def _halve_verdict() -> KillVerdict:
    return KillVerdict(
        action=KillAction.HALVE_SIZE,
        severity=KillSeverity.INFO,
        reason="funding veto soft",
        scope="bot:eth_perp",
        evidence={"symbol": "ETH-PERP", "bps": 22.0},
    )


# --------------------------------------------------------------------------- #
# Fresh-boot semantics
# --------------------------------------------------------------------------- #
class TestFreshBoot:
    def test_missing_latch_file_is_armed(self, tmp_path: Path) -> None:
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        assert not latch.path.exists()
        rec = latch.read()
        assert rec.state == LatchState.ARMED
        ok, reason = latch.boot_allowed()
        assert ok is True
        assert reason == "armed"

    def test_parent_dir_is_created(self, tmp_path: Path) -> None:
        nested = tmp_path / "state" / "nested" / "kill_latch.json"
        KillSwitchLatch(nested)
        assert nested.parent.is_dir()


# --------------------------------------------------------------------------- #
# Verdict latching
# --------------------------------------------------------------------------- #
class TestVerdictLatching:
    def test_flatten_all_trips_latch(self, tmp_path: Path) -> None:
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        changed = latch.record_verdict(_flatten_all_verdict())
        assert changed is True
        rec = latch.read()
        assert rec.state == LatchState.TRIPPED
        assert rec.reason == "daily loss 6.02% >= cap 6%"
        assert rec.action == "FLATTEN_ALL"
        assert rec.scope == "global"
        assert rec.tripped_at_utc is not None
        assert rec.evidence == {"daily_loss_pct": 6.02, "cap_pct": 6.0}

    def test_preemptive_trips_latch(self, tmp_path: Path) -> None:
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        assert latch.record_verdict(_preemptive_verdict()) is True
        assert latch.read().state == LatchState.TRIPPED

    def test_flatten_bot_does_NOT_trip_latch(self, tmp_path: Path) -> None:  # noqa: N802
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        changed = latch.record_verdict(_flatten_bot_verdict())
        assert changed is False
        assert latch.read().state == LatchState.ARMED

    def test_halve_size_does_NOT_trip_latch(self, tmp_path: Path) -> None:  # noqa: N802
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        assert latch.record_verdict(_halve_verdict()) is False
        assert latch.read().state == LatchState.ARMED

    def test_continue_verdict_does_NOT_trip_latch(self, tmp_path: Path) -> None:  # noqa: N802
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        ok_verdict = KillVerdict(
            action=KillAction.CONTINUE,
            severity=KillSeverity.INFO,
            reason="no trip",
            scope="global",
        )
        assert latch.record_verdict(ok_verdict) is False
        assert latch.read().state == LatchState.ARMED


# --------------------------------------------------------------------------- #
# First-trip-wins semantics
# --------------------------------------------------------------------------- #
class TestFirstTripWins:
    def test_second_trip_does_not_overwrite(self, tmp_path: Path) -> None:
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        latch.record_verdict(_flatten_all_verdict())
        rec1 = latch.read()
        first_reason = rec1.reason
        first_ts = rec1.tripped_at_utc

        # A second catastrophic verdict arrives; latch must hold on to the
        # ORIGINAL trip (that's the one that happened first in time).
        changed = latch.record_verdict(_preemptive_verdict())
        assert changed is False
        rec2 = latch.read()
        assert rec2.state == LatchState.TRIPPED
        assert rec2.reason == first_reason
        assert rec2.tripped_at_utc == first_ts


# --------------------------------------------------------------------------- #
# Boot gate
# --------------------------------------------------------------------------- #
class TestBootGate:
    def test_tripped_latch_refuses_boot(self, tmp_path: Path) -> None:
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        latch.record_verdict(_flatten_all_verdict())
        ok, reason = latch.boot_allowed()
        assert ok is False
        assert "TRIPPED" in reason
        assert "daily loss 6.02" in reason  # quoted original reason
        assert "clear_kill_switch" in reason  # tells operator how to clear

    def test_cleared_latch_allows_boot(self, tmp_path: Path) -> None:
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        latch.record_verdict(_flatten_all_verdict())
        latch.clear(cleared_by="edward")
        ok, reason = latch.boot_allowed()
        assert ok is True
        assert reason == "armed"


# --------------------------------------------------------------------------- #
# Clear semantics
# --------------------------------------------------------------------------- #
class TestClear:
    def test_clear_requires_operator_name(self, tmp_path: Path) -> None:
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        latch.record_verdict(_flatten_all_verdict())
        with pytest.raises(ValueError, match="cleared_by"):
            latch.clear(cleared_by="")
        with pytest.raises(ValueError, match="cleared_by"):
            latch.clear(cleared_by="   ")

    def test_clear_preserves_audit_trail(self, tmp_path: Path) -> None:
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        latch.record_verdict(_flatten_all_verdict())
        latch.clear(cleared_by="edward")
        rec = latch.read()
        # State is ARMED again
        assert rec.state == LatchState.ARMED
        # But the original trip metadata survives for post-mortem
        assert rec.reason == "daily loss 6.02% >= cap 6%"
        assert rec.action == "FLATTEN_ALL"
        assert rec.tripped_at_utc is not None
        assert rec.cleared_at_utc is not None
        assert rec.cleared_by == "edward"

    def test_clear_from_clean_latch_still_works(self, tmp_path: Path) -> None:
        """Clearing an already-ARMED latch is a no-op but safe."""
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        rec = latch.clear(cleared_by="edward")
        assert rec.state == LatchState.ARMED
        assert rec.cleared_by == "edward"


# --------------------------------------------------------------------------- #
# Corrupt-file defense
# --------------------------------------------------------------------------- #
class TestCorruptFile:
    def test_corrupt_json_fails_closed(self, tmp_path: Path) -> None:
        """A mangled latch file MUST NOT be trusted as ARMED."""
        p = tmp_path / "kill_latch.json"
        p.write_text("{ this is not json")
        latch = KillSwitchLatch(p)
        rec = latch.read()
        assert rec.state == LatchState.TRIPPED
        ok, reason = latch.boot_allowed()
        assert ok is False
        assert "corrupt" in reason.lower()

    def test_corrupt_file_can_be_cleared(self, tmp_path: Path) -> None:
        p = tmp_path / "kill_latch.json"
        p.write_text("not json")
        latch = KillSwitchLatch(p)
        # clear() reads the (corrupt) file, overwrites with a good ARMED
        # record; post-clear, boot is allowed.
        latch.clear(cleared_by="edward")
        ok, _ = latch.boot_allowed()
        assert ok is True


# --------------------------------------------------------------------------- #
# Atomicity
# --------------------------------------------------------------------------- #
class TestAtomicWrite:
    def test_write_leaves_no_tmp_file(self, tmp_path: Path) -> None:
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        latch.record_verdict(_flatten_all_verdict())
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_on_disk_json_is_well_formed(self, tmp_path: Path) -> None:
        latch = KillSwitchLatch(tmp_path / "kill_latch.json")
        latch.record_verdict(_flatten_all_verdict())
        raw = json.loads(latch.path.read_text(encoding="utf-8"))
        assert raw["state"] == "TRIPPED"
        assert raw["action"] == "FLATTEN_ALL"
        assert raw["scope"] == "global"
        assert raw["evidence"]["daily_loss_pct"] == 6.02
