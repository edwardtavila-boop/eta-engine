"""Unit tests for ``brain.avengers.mcc_intent_consumer``.

The consumer closes the operator-control loop between the MCC PWA's
intent files and the runtime fleet (kill-switch latch + paused-bots
set). These tests pin every drainer in isolation against a tmp_path-
scoped IntentPaths bag so nothing touches the operator's real
``~/.local/state/apex_predator/`` directory.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from apex_predator.brain.avengers.mcc_intent_consumer import (
    IntentPaths,
    consume_mcc_intents,
)
from apex_predator.core.kill_switch_latch import KillSwitchLatch, LatchState

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def paths(tmp_path: Path) -> IntentPaths:
    state_dir = tmp_path / "mcc_state"
    state_dir.mkdir()
    return IntentPaths.for_dir(state_dir)


@pytest.fixture
def latch(tmp_path: Path) -> KillSwitchLatch:
    return KillSwitchLatch(tmp_path / "kill_switch_latch.json")


# ---------------------------------------------------------------------------
# kill_request
# ---------------------------------------------------------------------------

class TestKillRequest:
    def test_trips_latch_and_unlinks_file(
        self, paths: IntentPaths, latch: KillSwitchLatch,
    ) -> None:
        paths.kill_request.write_text(json.dumps({
            "tripped_at": "2026-04-26T12:00:00Z",
            "operator":   "edward",
            "reason":     "manual trip via MCC",
            "scope":      "ALL",
        }))
        result = consume_mcc_intents(paths=paths, latch=latch)
        assert result.kill_tripped is True
        assert result.errors == []
        # File is unlinked so a re-tick doesn't re-apply the intent.
        assert not paths.kill_request.exists()
        # Latch records the verdict.
        rec = latch.read()
        assert rec.state == LatchState.TRIPPED
        assert "edward" in (rec.reason or "")

    def test_missing_file_is_noop(
        self, paths: IntentPaths, latch: KillSwitchLatch,
    ) -> None:
        result = consume_mcc_intents(paths=paths, latch=latch)
        assert result.kill_tripped is False
        assert result.errors == []

    def test_corrupt_json_leaves_file_in_place(
        self, paths: IntentPaths, latch: KillSwitchLatch,
    ) -> None:
        paths.kill_request.write_text("{ this is not json")
        result = consume_mcc_intents(paths=paths, latch=latch)
        assert result.kill_tripped is False
        assert any("kill_request parse" in e for e in result.errors)
        # File NOT unlinked -- operator can fix it manually.
        assert paths.kill_request.exists()
        # Latch unchanged.
        assert latch.read().state == LatchState.ARMED


# ---------------------------------------------------------------------------
# kill_clear_request
# ---------------------------------------------------------------------------

class TestKillClearRequest:
    def test_clears_latch_and_unlinks_file(
        self, paths: IntentPaths, latch: KillSwitchLatch,
    ) -> None:
        # Prime the latch tripped first.
        from apex_predator.core.kill_switch_runtime import (
            KillAction,
            KillSeverity,
            KillVerdict,
        )
        latch.record_verdict(KillVerdict(
            action=KillAction.FLATTEN_ALL,
            severity=KillSeverity.CRITICAL,
            reason="prior trip",
            scope="global",
        ))
        assert latch.read().state == LatchState.TRIPPED

        paths.kill_clear_request.write_text(json.dumps({
            "operator": "edward",
            "reason":   "operator reset via MCC",
        }))
        result = consume_mcc_intents(paths=paths, latch=latch)
        assert result.kill_cleared is True
        assert not paths.kill_clear_request.exists()
        assert latch.read().state == LatchState.ARMED


# ---------------------------------------------------------------------------
# pause_requests
# ---------------------------------------------------------------------------

class TestPauseRequests:
    def _line(self, intent: str, bot_id: str, operator: str = "edward") -> str:
        return json.dumps({
            "ts":       "2026-04-26T12:00:00Z",
            "intent":   intent,
            "bot_id":   bot_id,
            "operator": operator,
            "reason":   "test",
        })

    def test_first_pass_picks_up_all_lines(
        self, paths: IntentPaths, latch: KillSwitchLatch,
    ) -> None:
        paths.pause_requests.write_text(
            self._line("pause", "mnq") + "\n"
            + self._line("pause", "eth_perp") + "\n"
            + self._line("unpause", "mnq") + "\n",
            encoding="utf-8",
        )
        result = consume_mcc_intents(paths=paths, latch=latch)
        assert result.pause_applied == 2
        assert result.unpause_applied == 1
        assert result.paused_bots_now == ["eth_perp"]
        # Offset persisted so re-running is idempotent.
        assert paths.pause_offset.exists()

    def test_idempotent_across_invocations(
        self, paths: IntentPaths, latch: KillSwitchLatch,
    ) -> None:
        paths.pause_requests.write_text(
            self._line("pause", "mnq") + "\n", encoding="utf-8",
        )
        first = consume_mcc_intents(paths=paths, latch=latch)
        assert first.pause_applied == 1
        # Second invocation with no new lines must apply 0.
        second = consume_mcc_intents(paths=paths, latch=latch)
        assert second.pause_applied == 0
        assert second.unpause_applied == 0
        assert second.paused_bots_now == ["mnq"]

    def test_appended_lines_picked_up_on_next_tick(
        self, paths: IntentPaths, latch: KillSwitchLatch,
    ) -> None:
        paths.pause_requests.write_text(
            self._line("pause", "mnq") + "\n", encoding="utf-8",
        )
        consume_mcc_intents(paths=paths, latch=latch)
        # Operator presses unpause via MCC -> appends a line.
        with paths.pause_requests.open("a", encoding="utf-8") as fh:
            fh.write(self._line("unpause", "mnq") + "\n")
        result = consume_mcc_intents(paths=paths, latch=latch)
        assert result.pause_applied == 0
        assert result.unpause_applied == 1
        assert result.paused_bots_now == []

    def test_malformed_line_skipped_with_error(
        self, paths: IntentPaths, latch: KillSwitchLatch,
    ) -> None:
        paths.pause_requests.write_text(
            self._line("pause", "mnq") + "\n"
            + "this is not json\n"
            + self._line("pause", "eth_perp") + "\n",
            encoding="utf-8",
        )
        result = consume_mcc_intents(paths=paths, latch=latch)
        assert result.pause_applied == 2
        assert any("bad json" in e for e in result.errors)
        assert sorted(result.paused_bots_now) == ["eth_perp", "mnq"]

    def test_unknown_intent_logged_no_state_change(
        self, paths: IntentPaths, latch: KillSwitchLatch,
    ) -> None:
        paths.pause_requests.write_text(
            json.dumps({"intent": "burninate", "bot_id": "mnq"}) + "\n",
            encoding="utf-8",
        )
        result = consume_mcc_intents(paths=paths, latch=latch)
        assert result.pause_applied == 0
        assert result.unpause_applied == 0
        assert any("unknown intent" in e for e in result.errors)

    def test_paused_bots_set_persists_across_invocations(
        self, paths: IntentPaths, latch: KillSwitchLatch,
    ) -> None:
        paths.pause_requests.write_text(
            self._line("pause", "mnq") + "\n"
            + self._line("pause", "eth_perp") + "\n",
            encoding="utf-8",
        )
        consume_mcc_intents(paths=paths, latch=latch)
        # Independent invocation reads the persisted set even with
        # no new pause-request lines.
        result = consume_mcc_intents(paths=paths, latch=latch)
        assert sorted(result.paused_bots_now) == ["eth_perp", "mnq"]


# ---------------------------------------------------------------------------
# Combined invocation
# ---------------------------------------------------------------------------

class TestCombined:
    def test_drains_all_three_in_one_call(
        self, paths: IntentPaths, latch: KillSwitchLatch,
    ) -> None:
        paths.kill_request.write_text(json.dumps({
            "operator": "edward", "reason": "x", "scope": "ALL",
        }))
        paths.pause_requests.write_text(
            json.dumps({
                "intent": "pause", "bot_id": "mnq", "operator": "edward",
            }) + "\n",
            encoding="utf-8",
        )
        result = consume_mcc_intents(paths=paths, latch=latch)
        assert result.kill_tripped is True
        assert result.pause_applied == 1
        assert result.paused_bots_now == ["mnq"]
        assert not paths.kill_request.exists()
