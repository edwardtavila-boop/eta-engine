"""Tests for ``apex_predator.core.mcc_intent``.

Exercise:
* read_kill_request / clear_kill_request round-trip + corruption tolerance
* kill_request_as_verdict construction (FLATTEN_ALL / CRITICAL, source=mcc)
* read_pause_requests + latest_pause_intent tail-wins semantics
* apply_pause_intent overrides

The module touches the real filesystem -- every test redirects the
module-level paths into a tmp_path via monkeypatch, so the suite
NEVER touches a real operator state directory.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from apex_predator.core import mcc_intent

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def mcc_state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mcc_intent, "KILL_REQUEST", tmp_path / "kill.json")
    monkeypatch.setattr(mcc_intent, "PAUSE_REQUESTS", tmp_path / "pause.jsonl")
    monkeypatch.setattr(mcc_intent, "ALERT_ACKS", tmp_path / "acks.jsonl")
    return tmp_path


# ---------------------------------------------------------------------------
# Kill request
# ---------------------------------------------------------------------------


class TestKillRequest:
    def test_no_file_returns_none(self, mcc_state: Path) -> None:
        assert mcc_intent.read_kill_request() is None
        assert mcc_intent.kill_request_as_verdict() is None

    def test_round_trip_parses_record(self, mcc_state: Path) -> None:
        rec = {
            "tripped_at": "2026-04-26T20:00:00+00:00",
            "operator": "ops@example.com",
            "reason": "drift detector raised AUTO_DEMOTE",
            "scope": "global",
        }
        (mcc_state / "kill.json").write_text(json.dumps(rec), encoding="utf-8")
        out = mcc_intent.read_kill_request()
        assert out == rec

    def test_corrupt_file_returns_none(self, mcc_state: Path) -> None:
        (mcc_state / "kill.json").write_text("not json", encoding="utf-8")
        assert mcc_intent.read_kill_request() is None

    def test_kill_request_as_verdict_builds_flatten_all_critical(self, mcc_state: Path) -> None:
        from apex_predator.core.kill_switch_runtime import KillAction, KillSeverity

        rec = {
            "tripped_at": "2026-04-26T20:00:00+00:00",
            "operator": "ops@example.com",
            "reason": "manual ops trip",
        }
        (mcc_state / "kill.json").write_text(json.dumps(rec), encoding="utf-8")
        v = mcc_intent.kill_request_as_verdict()
        assert v is not None
        assert v.action is KillAction.FLATTEN_ALL
        assert v.severity is KillSeverity.CRITICAL
        assert v.reason == "manual ops trip"
        assert v.scope == "global"
        assert v.evidence["source"] == "mcc"
        assert v.evidence["operator"] == "ops@example.com"
        assert v.evidence["tripped_at"] == "2026-04-26T20:00:00+00:00"

    def test_clear_kill_request_removes_file(self, mcc_state: Path) -> None:
        (mcc_state / "kill.json").write_text(json.dumps({"reason": "x"}), encoding="utf-8")
        assert mcc_intent.clear_kill_request() is True
        assert not (mcc_state / "kill.json").exists()

    def test_clear_kill_request_idempotent_when_missing(self, mcc_state: Path) -> None:
        # Pre-condition: file does not exist.
        assert mcc_intent.clear_kill_request() is False
        # Calling again is still safe.
        assert mcc_intent.clear_kill_request() is False

    def test_default_reason_when_missing(self, mcc_state: Path) -> None:
        (mcc_state / "kill.json").write_text(json.dumps({}), encoding="utf-8")
        v = mcc_intent.kill_request_as_verdict()
        assert v is not None
        assert "manual operator trip via MCC" in v.reason


# ---------------------------------------------------------------------------
# Pause / unpause intent
# ---------------------------------------------------------------------------


class TestPauseIntent:
    def test_no_file_returns_empty(self, mcc_state: Path) -> None:
        assert mcc_intent.read_pause_requests() == []
        assert mcc_intent.latest_pause_intent("mnq") is None

    def test_latest_pause_wins(self, mcc_state: Path) -> None:
        path = mcc_state / "pause.jsonl"
        path.write_text(
            json.dumps({"ts": "1", "intent": "pause", "bot_id": "mnq"})
            + "\n"
            + json.dumps({"ts": "2", "intent": "unpause", "bot_id": "mnq"})
            + "\n"
            + json.dumps({"ts": "3", "intent": "pause", "bot_id": "mnq"})
            + "\n",
            encoding="utf-8",
        )
        assert mcc_intent.latest_pause_intent("mnq") == "pause"

    def test_per_bot_isolation(self, mcc_state: Path) -> None:
        path = mcc_state / "pause.jsonl"
        path.write_text(
            json.dumps({"intent": "pause", "bot_id": "mnq"})
            + "\n"
            + json.dumps({"intent": "unpause", "bot_id": "btc_hybrid"})
            + "\n",
            encoding="utf-8",
        )
        assert mcc_intent.latest_pause_intent("mnq") == "pause"
        assert mcc_intent.latest_pause_intent("btc_hybrid") == "unpause"
        assert mcc_intent.latest_pause_intent("eth_perp") is None

    def test_malformed_lines_skipped(self, mcc_state: Path) -> None:
        path = mcc_state / "pause.jsonl"
        path.write_text(
            "not json\n" + json.dumps({"intent": "pause", "bot_id": "mnq"}) + "\n" + "{garbage\n",
            encoding="utf-8",
        )
        assert mcc_intent.latest_pause_intent("mnq") == "pause"

    def test_apply_pause_intent_pause_overrides_current_false(self, mcc_state: Path) -> None:
        (mcc_state / "pause.jsonl").write_text(
            json.dumps({"intent": "pause", "bot_id": "mnq"}) + "\n",
            encoding="utf-8",
        )
        assert mcc_intent.apply_pause_intent("mnq", current_paused=False) is True

    def test_apply_pause_intent_unpause_overrides_current_true(self, mcc_state: Path) -> None:
        (mcc_state / "pause.jsonl").write_text(
            json.dumps({"intent": "unpause", "bot_id": "mnq"}) + "\n",
            encoding="utf-8",
        )
        assert mcc_intent.apply_pause_intent("mnq", current_paused=True) is False

    def test_apply_pause_intent_no_record_preserves_current(self, mcc_state: Path) -> None:
        # No file at all.
        assert mcc_intent.apply_pause_intent("mnq", current_paused=True) is True
        assert mcc_intent.apply_pause_intent("mnq", current_paused=False) is False
        # File exists but no record for this bot.
        (mcc_state / "pause.jsonl").write_text(
            json.dumps({"intent": "pause", "bot_id": "btc_hybrid"}) + "\n",
            encoding="utf-8",
        )
        assert mcc_intent.apply_pause_intent("mnq", current_paused=True) is True

    def test_unknown_intent_value_ignored(self, mcc_state: Path) -> None:
        # Defensive: unknown intent strings shouldn't change pause state.
        (mcc_state / "pause.jsonl").write_text(
            json.dumps({"intent": "maybe", "bot_id": "mnq"}) + "\n",
            encoding="utf-8",
        )
        assert mcc_intent.latest_pause_intent("mnq") is None
        assert mcc_intent.apply_pause_intent("mnq", current_paused=False) is False


# ---------------------------------------------------------------------------
# BaseBot integration: check_risk respects MCC pause intent
# ---------------------------------------------------------------------------


class TestBaseBotIntegration:
    """Confirm BaseBot.check_risk consults MCC intent before evaluating."""

    def _bot(self, name: str = "mnq"):
        from apex_predator.bots.base_bot import BaseBot, BotConfig, Tier

        class _Concrete(BaseBot):
            async def start(self) -> None: ...
            async def stop(self) -> None: ...
            async def on_bar(self, bar) -> None: ...
            async def on_signal(self, signal) -> None: ...
            def evaluate_entry(self, bar, confluence_score) -> bool:
                return True

            def evaluate_exit(self, position) -> bool:
                return False

        cfg = BotConfig(
            name=name,
            symbol="MNQ",
            tier=Tier.FUTURES,
            baseline_usd=50_000,
            starting_capital_usd=50_000,
        )
        return _Concrete(cfg)

    def test_mcc_pause_intent_halts_check_risk(self, mcc_state: Path) -> None:
        bot = self._bot("mnq")
        # Sanity: with no MCC file, healthy bot trades.
        assert bot.check_risk() is True

        # Now MCC has paused this bot.
        (mcc_state / "pause.jsonl").write_text(
            json.dumps({"intent": "pause", "bot_id": "mnq"}) + "\n",
            encoding="utf-8",
        )
        assert bot.check_risk() is False
        assert bot.state.is_paused is True

    def test_mcc_unpause_clears_local_pause(self, mcc_state: Path) -> None:
        bot = self._bot("mnq")
        bot.state.is_paused = True
        # Without MCC intent, local pause stays True and check_risk halts.
        assert bot.check_risk() is False

        # Operator unpause via MCC overrides local.
        (mcc_state / "pause.jsonl").write_text(
            json.dumps({"intent": "unpause", "bot_id": "mnq"}) + "\n",
            encoding="utf-8",
        )
        assert bot.check_risk() is True
        assert bot.state.is_paused is False
