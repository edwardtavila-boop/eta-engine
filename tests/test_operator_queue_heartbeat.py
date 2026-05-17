from __future__ import annotations

import json

import pytest

from eta_engine.scripts import jarvis_status, operator_queue_heartbeat


def test_feed_heartbeat_entrypoint_delegates_to_canonical_script() -> None:
    from eta_engine.feeds import operator_queue_heartbeat as feed_heartbeat

    assert feed_heartbeat.build_snapshot_with_drift is operator_queue_heartbeat.build_snapshot_with_drift
    assert feed_heartbeat.build_heartbeat is operator_queue_heartbeat.build_heartbeat


def _queue(blocked: int, *, op_id: str | None = "OP-18", action: str | None = "fix it") -> dict[str, object]:
    blockers = [{"op_id": op_id}] if op_id else []
    actions = [action] if action else []
    return {
        "summary": {"BLOCKED": blocked},
        "top_blockers": blockers,
        "next_actions": actions,
        "error": None,
    }


def _readiness(*, blocked_data: int = 0, paper_ready: int = 10) -> dict[str, object]:
    return {
        "status": "ready" if blocked_data == 0 else "blocked",
        "summary": {
            "blocked_data": blocked_data,
            "can_paper_trade": paper_ready,
            "launch_lanes": {"blocked_data": blocked_data, "paper_soak": paper_ready},
        },
        "top_actions": [],
    }


def _second_brain(*, episodes: int = 42, eligible_patterns: int = 2) -> dict[str, object]:
    return {
        "source": "jarvis_status.second_brain",
        "status": "warm",
        "n_episodes": episodes,
        "win_rate": 0.61,
        "avg_r": 0.27,
        "semantic_patterns": eligible_patterns,
        "procedural_versions": 3,
        "playbook": {
            "eligible_patterns": eligible_patterns,
            "favor_patterns": [{"key": "trend_pullback"}],
            "avoid_patterns": [],
        },
        "truth_note": "canonical memory under EvolutionaryTradingAlgo",
        "legacy_sources_active": False,
        "top_patterns": [],
    }


@pytest.fixture(autouse=True)
def _patch_second_brain(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(jarvis_status, "build_second_brain_summary", lambda **_kwargs: _second_brain())


def test_build_heartbeat_marks_notify_from_drift() -> None:
    heartbeat = operator_queue_heartbeat.build_heartbeat(
        {
            "generated_at": "2026-04-29T00:00:00+00:00",
            "status": "blocked",
            "blocked_count": 2,
            "first_blocker_op_id": "OP-18",
            "first_next_action": "fix it",
            "bot_strategy_readiness_status": "ready",
            "bot_strategy_blocked_data": 0,
            "bot_strategy_paper_ready": 10,
            "second_brain_status": "warm",
            "second_brain_episodes": 42,
            "second_brain_eligible_patterns": 2,
            "second_brain_legacy_sources_active": False,
            "drift": {
                "changed": True,
                "summary": "operator queue drift detected: blocked_count",
                "changed_fields": ["blocked_count"],
                "blocked_count_delta": 1,
                "second_brain_episodes_delta": 2,
                "second_brain_eligible_patterns_delta": 1,
            },
        },
        None,
    )

    assert heartbeat["notify"] is True
    assert heartbeat["drift_changed"] is True
    assert heartbeat["changed_fields"] == ["blocked_count"]
    assert heartbeat["blocked_count_delta"] == 1
    assert heartbeat["bot_strategy_readiness_status"] == "ready"
    assert heartbeat["bot_strategy_blocked_data"] == 0
    assert heartbeat["bot_strategy_paper_ready"] == 10
    assert heartbeat["second_brain_status"] == "warm"
    assert heartbeat["second_brain_episodes"] == 42
    assert heartbeat["second_brain_eligible_patterns"] == 2
    assert heartbeat["second_brain_legacy_sources_active"] is False
    assert heartbeat["second_brain_episodes_delta"] == 2
    assert heartbeat["second_brain_eligible_patterns_delta"] == 1


def test_render_text_includes_bot_readiness_fields() -> None:
    line = operator_queue_heartbeat.render_text(
        {
            "notify": False,
            "status": "clear",
            "blocked_count": 0,
            "first_blocker_op_id": None,
            "first_next_action": None,
            "changed_fields": [],
            "drift_summary": "operator queue unchanged",
            "bot_strategy_readiness_status": "ready",
            "bot_strategy_blocked_data": 0,
            "bot_strategy_paper_ready": 10,
            "second_brain_status": "warm",
            "second_brain_episodes": 42,
            "second_brain_eligible_patterns": 2,
        }
    )

    assert "bot_readiness=ready" in line
    assert "bot_blocked_data=0" in line
    assert "bot_paper_ready=10" in line
    assert "second_brain=warm" in line
    assert "second_brain_episodes=42" in line
    assert "second_brain_eligible=2" in line


def test_main_changed_only_suppresses_unchanged_output(monkeypatch, capsys, tmp_path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "operator_queue_snapshot.json"
    target.write_text(
        json.dumps(
            {
                "status": "blocked",
                "blocked_count": 1,
                "first_blocker_op_id": "OP-18",
                "first_next_action": "fix it",
                "bot_strategy_readiness_status": "ready",
                "bot_strategy_blocked_data": 0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(jarvis_status, "build_operator_queue_summary", lambda **_kwargs: _queue(1))
    monkeypatch.setattr(jarvis_status, "build_bot_strategy_readiness_summary", lambda **_kwargs: _readiness())

    rc = operator_queue_heartbeat.main(["--out", str(target), "--changed-only"])

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_changed_only_emits_json_when_drift_changes(monkeypatch, capsys, tmp_path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "operator_queue_snapshot.json"
    target.write_text(
        json.dumps(
            {
                "status": "blocked",
                "blocked_count": 1,
                "first_blocker_op_id": "OP-18",
                "first_next_action": "fix it",
                "bot_strategy_readiness_status": "ready",
                "bot_strategy_blocked_data": 0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(jarvis_status, "build_operator_queue_summary", lambda **_kwargs: _queue(2))
    monkeypatch.setattr(jarvis_status, "build_bot_strategy_readiness_summary", lambda **_kwargs: _readiness())

    rc = operator_queue_heartbeat.main(["--out", str(target), "--changed-only", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["notify"] is True
    assert payload["changed_fields"] == ["blocked_count"]
    assert payload["blocked_count_delta"] == 1


def test_main_strict_drift_returns_three(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "operator_queue_snapshot.json"
    target.write_text(
        json.dumps(
            {
                "status": "clear",
                "blocked_count": 0,
                "first_blocker_op_id": None,
                "first_next_action": None,
                "bot_strategy_readiness_status": "ready",
                "bot_strategy_blocked_data": 0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(jarvis_status, "build_operator_queue_summary", lambda **_kwargs: _queue(1))
    monkeypatch.setattr(jarvis_status, "build_bot_strategy_readiness_summary", lambda **_kwargs: _readiness())

    rc = operator_queue_heartbeat.main(["--out", str(target), "--strict-drift"])

    assert rc == 3


def test_main_strict_blockers_returns_two(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(jarvis_status, "build_operator_queue_summary", lambda **_kwargs: _queue(1))
    monkeypatch.setattr(jarvis_status, "build_bot_strategy_readiness_summary", lambda **_kwargs: _readiness())

    rc = operator_queue_heartbeat.main(["--out", str(tmp_path / "snapshot.json"), "--strict-blockers"])

    assert rc == 2
