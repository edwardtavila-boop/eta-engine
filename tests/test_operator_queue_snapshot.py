from __future__ import annotations

import json

import pytest

from eta_engine.scripts import jarvis_status, operator_queue_snapshot, workspace_roots


def test_feed_snapshot_entrypoint_delegates_to_canonical_script() -> None:
    from eta_engine.feeds import operator_queue_snapshot as feed_snapshot

    assert feed_snapshot.build_snapshot is operator_queue_snapshot.build_snapshot
    assert feed_snapshot.write_snapshot is operator_queue_snapshot.write_snapshot


def _readiness(
    *,
    status: str = "ready",
    blocked_data: int = 0,
    paper_ready: int = 10,
) -> dict[str, object]:
    return {
        "source": "bot_strategy_readiness",
        "status": status,
        "summary": {
            "blocked_data": blocked_data,
            "can_live_any": False,
            "can_paper_trade": paper_ready,
            "launch_lanes": {"blocked_data": blocked_data, "paper_soak": paper_ready},
        },
        "top_actions": [],
    }


def _second_brain(
    *,
    status: str = "warm",
    episodes: int = 42,
    eligible_patterns: int = 2,
    favor_patterns: int = 1,
    avoid_patterns: int = 1,
    legacy_sources_active: bool = False,
) -> dict[str, object]:
    return {
        "source": "jarvis_status.second_brain",
        "status": status,
        "n_episodes": episodes,
        "win_rate": 0.61,
        "avg_r": 0.27,
        "semantic_patterns": eligible_patterns,
        "procedural_versions": 3,
        "playbook": {
            "eligible_patterns": eligible_patterns,
            "favor_patterns": [{"key": "trend_pullback"} for _ in range(favor_patterns)],
            "avoid_patterns": [{"key": "late_chop"} for _ in range(avoid_patterns)],
        },
        "truth_note": "canonical memory under EvolutionaryTradingAlgo",
        "legacy_sources_active": legacy_sources_active,
        "top_patterns": [],
    }


@pytest.fixture(autouse=True)
def _patch_second_brain(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(jarvis_status, "build_second_brain_summary", lambda **_kwargs: _second_brain())


def test_build_snapshot_summarizes_top_blocker(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        jarvis_status,
        "build_operator_queue_summary",
        lambda **_kwargs: {
            "summary": {"BLOCKED": 2, "OBSERVED": 1, "UNKNOWN": 0, "DONE": 0},
            "top_blockers": [{"op_id": "OP-18", "title": "Resolve DR blockers"}],
            "next_actions": ["cp .env.example .env && chmod 600 .env"],
            "launch_blocked_count": 1,
            "top_launch_blockers": [{"op_id": "OP-18", "title": "Resolve DR blockers"}],
            "launch_next_actions": ["cp .env.example .env && chmod 600 .env"],
            "error": None,
        },
    )
    monkeypatch.setattr(jarvis_status, "build_bot_strategy_readiness_summary", lambda **_kwargs: _readiness())

    snapshot = operator_queue_snapshot.build_snapshot(limit=3)

    assert snapshot["schema_version"] == 1
    assert snapshot["source"] == "jarvis_status.operator_queue"
    assert snapshot["status"] == "blocked"
    assert snapshot["blocked_count"] == 2
    assert snapshot["first_blocker_op_id"] == "OP-18"
    assert snapshot["first_next_action"] == "cp .env.example .env && chmod 600 .env"
    assert snapshot["launch_status"] == "blocked"
    assert snapshot["launch_blocked_count"] == 1
    assert snapshot["non_launch_blocked_count"] == 1
    assert snapshot["first_launch_blocker_op_id"] == "OP-18"
    assert snapshot["first_launch_next_action"] == "cp .env.example .env && chmod 600 .env"
    assert snapshot["bot_strategy_readiness_status"] == "ready"
    assert snapshot["bot_strategy_blocked_data"] == 0
    assert snapshot["bot_strategy_paper_ready"] == 10
    assert snapshot["bot_strategy_readiness"]["summary"]["can_paper_trade"] == 10
    assert snapshot["second_brain_status"] == "warm"
    assert snapshot["second_brain_episodes"] == 42
    assert snapshot["second_brain_eligible_patterns"] == 2
    assert snapshot["second_brain_favor_patterns"] == 1
    assert snapshot["second_brain_avoid_patterns"] == 1
    assert snapshot["second_brain"]["playbook"]["eligible_patterns"] == 2


def test_build_snapshot_can_refresh_supervisor_pinned_readiness(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        jarvis_status,
        "build_operator_queue_summary",
        lambda **_kwargs: {
            "summary": {"BLOCKED": 0, "OBSERVED": 1, "UNKNOWN": 0, "DONE": 0},
            "top_blockers": [],
            "next_actions": [],
            "error": None,
        },
    )
    monkeypatch.setattr(
        operator_queue_snapshot,
        "_build_current_readiness_summary",
        lambda **_kwargs: _readiness(paper_ready=12),
    )

    snapshot = operator_queue_snapshot.build_snapshot(limit=3, refresh_readiness=True)

    assert snapshot["status"] == "clear"
    assert snapshot["bot_strategy_paper_ready"] == 12
    assert snapshot["bot_strategy_readiness"]["summary"]["can_paper_trade"] == 12


def test_readiness_top_actions_follow_capital_priority() -> None:
    actions = operator_queue_snapshot._readiness_top_actions(
        [
            {
                "bot_id": "sol_optimized",
                "strategy_id": "sol_optimized_v1",
                "launch_lane": "paper_soak",
                "next_action": "Run paper-soak and broker drift checks.",
                "can_paper_trade": True,
                "can_live_trade": False,
                "priority_bucket": "spot_crypto",
                "capital_priority": 9001,
            },
            {
                "bot_id": "volume_profile_nq",
                "strategy_id": "volume_profile_nq_v1",
                "launch_lane": "paper_soak",
                "next_action": "Run paper-soak and broker drift checks.",
                "can_paper_trade": True,
                "can_live_trade": False,
                "priority_bucket": "equity_index_futures",
                "preferred_broker_stack": ["ibkr", "tradovate_when_enabled", "tastytrade"],
                "capital_priority": 1001,
            },
        ],
        limit=2,
    )

    assert [row["bot_id"] for row in actions] == ["volume_profile_nq", "sol_optimized"]
    assert actions[0]["priority_bucket"] == "equity_index_futures"
    assert actions[0]["preferred_broker_stack"] == ["ibkr", "tradovate_when_enabled", "tastytrade"]


def test_write_snapshot_uses_atomic_temp_then_target(tmp_path) -> None:
    target = tmp_path / "state" / "operator_queue_snapshot.json"
    snapshot = {
        "schema_version": 1,
        "generated_at": "2026-04-29T00:00:00+00:00",
        "source": "test",
        "status": "clear",
        "blocked_count": 0,
        "operator_queue": {"summary": {"BLOCKED": 0}},
    }

    written = operator_queue_snapshot.write_snapshot(snapshot, target, previous_path=tmp_path / "previous.json")

    assert written == target
    assert not target.with_suffix(".json.tmp").exists()
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "clear"


def test_write_snapshot_preserves_previous_target(tmp_path) -> None:
    target = tmp_path / "state" / "operator_queue_snapshot.json"
    previous = tmp_path / "state" / "operator_queue_snapshot.previous.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "blocked",
                "blocked_count": 3,
                "first_blocker_op_id": "OP-18",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    operator_queue_snapshot.write_snapshot(
        {
            "schema_version": 1,
            "generated_at": "2026-04-29T00:01:00+00:00",
            "source": "test",
            "status": "clear",
            "blocked_count": 0,
        },
        target,
        previous_path=previous,
    )

    assert json.loads(previous.read_text(encoding="utf-8"))["blocked_count"] == 3
    assert json.loads(target.read_text(encoding="utf-8"))["blocked_count"] == 0


def test_cli_rejects_output_path_outside_workspace(monkeypatch, tmp_path) -> None:
    fake_workspace = tmp_path / "workspace"
    outside_workspace = tmp_path / "outside" / "operator_queue_snapshot.json"
    fake_workspace.mkdir()
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", fake_workspace)
    monkeypatch.setattr(
        operator_queue_snapshot,
        "build_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("snapshot should not build")),
    )

    with pytest.raises(SystemExit) as exc:
        operator_queue_snapshot.main(["--out", str(outside_workspace)])

    assert exc.value.code == 2


def test_compare_snapshots_reports_changed_fields() -> None:
    previous = {
        "status": "blocked",
        "launch_status": "blocked",
        "blocked_count": 2,
        "launch_blocked_count": 2,
        "first_blocker_op_id": "OP-18",
        "first_launch_blocker_op_id": "OP-18",
        "first_next_action": "old",
        "first_launch_next_action": "old launch",
    }
    current = {
        "status": "blocked",
        "launch_status": "clear",
        "blocked_count": 3,
        "launch_blocked_count": 0,
        "first_blocker_op_id": "OP-18",
        "first_launch_blocker_op_id": None,
        "first_next_action": "new",
        "first_launch_next_action": None,
    }

    drift = operator_queue_snapshot.compare_snapshots(previous, current)

    assert drift["previous_present"] is True
    assert drift["changed"] is True
    assert drift["blocked_count_delta"] == 1
    assert drift["launch_blocked_count_delta"] == -2
    assert drift["changed_fields"] == [
        "blocked_count",
        "launch_blocked_count",
        "launch_status",
        "first_launch_blocker_op_id",
        "first_next_action",
        "first_launch_next_action",
    ]


def test_compare_snapshots_reports_bot_readiness_drift() -> None:
    previous = {
        "status": "blocked",
        "blocked_count": 2,
        "first_blocker_op_id": "OP-18",
        "first_next_action": "same",
        "bot_strategy_readiness_status": "ready",
        "bot_strategy_blocked_data": 0,
    }
    current = {
        **previous,
        "bot_strategy_readiness_status": "blocked",
        "bot_strategy_blocked_data": 2,
    }

    drift = operator_queue_snapshot.compare_snapshots(previous, current)

    assert drift["changed"] is True
    assert drift["bot_strategy_blocked_data_delta"] == 2
    assert drift["changed_fields"] == [
        "bot_strategy_readiness_status",
        "bot_strategy_blocked_data",
    ]


def test_compare_snapshots_reports_second_brain_drift() -> None:
    previous = {
        "status": "clear",
        "blocked_count": 0,
        "first_blocker_op_id": None,
        "first_next_action": None,
        "second_brain_status": "cold",
        "second_brain_episodes": 40,
        "second_brain_eligible_patterns": 1,
        "second_brain_legacy_sources_active": True,
    }
    current = {
        **previous,
        "second_brain_status": "warm",
        "second_brain_episodes": 42,
        "second_brain_eligible_patterns": 2,
        "second_brain_legacy_sources_active": False,
    }

    drift = operator_queue_snapshot.compare_snapshots(previous, current)

    assert drift["changed"] is True
    assert drift["second_brain_status_changed"] is True
    assert drift["second_brain_episodes_delta"] == 2
    assert drift["second_brain_eligible_patterns_delta"] == 1
    assert drift["second_brain_legacy_sources_changed"] is True
    assert drift["changed_fields"] == [
        "second_brain_status",
        "second_brain_episodes",
        "second_brain_eligible_patterns",
        "second_brain_legacy_sources_active",
    ]


def test_second_brain_summary_fails_soft(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        jarvis_status,
        "build_second_brain_summary",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("memory unavailable")),
    )

    payload = operator_queue_snapshot._second_brain_summary(limit=3)

    assert payload["status"] == "unavailable"
    assert payload["n_episodes"] == 0
    assert "memory unavailable" in str(payload["error"])


def test_compare_snapshots_reports_unchanged() -> None:
    previous = {
        "status": "blocked",
        "blocked_count": 2,
        "first_blocker_op_id": "OP-18",
        "first_next_action": "same",
        "bot_strategy_readiness_status": "ready",
        "bot_strategy_blocked_data": 0,
    }
    current = dict(previous)

    drift = operator_queue_snapshot.compare_snapshots(previous, current)

    assert drift["changed"] is False
    assert drift["changed_fields"] == []
    assert drift["summary"] == "operator queue unchanged"


def test_custom_out_uses_sibling_previous_path(tmp_path) -> None:
    target = tmp_path / "custom_snapshot.json"

    previous = operator_queue_snapshot.default_previous_path_for(target)

    assert previous == tmp_path / "custom_snapshot.previous.json"


def test_main_no_write_json_does_not_create_default(monkeypatch, capsys, tmp_path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "operator_queue_snapshot.json"
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(
        jarvis_status,
        "build_operator_queue_summary",
        lambda **_kwargs: {
            "summary": {"BLOCKED": 0},
            "top_blockers": [],
            "next_actions": [],
            "error": None,
        },
    )
    monkeypatch.setattr(jarvis_status, "build_bot_strategy_readiness_summary", lambda **_kwargs: _readiness())

    rc = operator_queue_snapshot.main(["--out", str(target), "--json", "--no-write"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "clear"
    assert payload["drift"]["previous_present"] is False
    assert not target.exists()


def test_main_strict_returns_two_when_blocked(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(
        jarvis_status,
        "build_operator_queue_summary",
        lambda **_kwargs: {
            "summary": {"BLOCKED": 1},
            "top_blockers": [{"op_id": "OP-18"}],
            "next_actions": ["fix it"],
            "error": None,
        },
    )
    monkeypatch.setattr(jarvis_status, "build_bot_strategy_readiness_summary", lambda **_kwargs: _readiness())

    rc = operator_queue_snapshot.main(["--out", str(tmp_path / "snapshot.json"), "--strict"])

    assert rc == 2
