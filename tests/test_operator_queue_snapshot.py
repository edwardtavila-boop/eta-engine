from __future__ import annotations

import json

from eta_engine.scripts import jarvis_status, operator_queue_snapshot, workspace_roots


def test_build_snapshot_summarizes_top_blocker(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        jarvis_status,
        "build_operator_queue_summary",
        lambda **_kwargs: {
            "summary": {"BLOCKED": 2, "OBSERVED": 1, "UNKNOWN": 0, "DONE": 0},
            "top_blockers": [{"op_id": "OP-18", "title": "Resolve DR blockers"}],
            "next_actions": ["cp .env.example .env && chmod 600 .env"],
            "error": None,
        },
    )

    snapshot = operator_queue_snapshot.build_snapshot(limit=3)

    assert snapshot["schema_version"] == 1
    assert snapshot["source"] == "jarvis_status.operator_queue"
    assert snapshot["status"] == "blocked"
    assert snapshot["blocked_count"] == 2
    assert snapshot["first_blocker_op_id"] == "OP-18"
    assert snapshot["first_next_action"] == "cp .env.example .env && chmod 600 .env"


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

    written = operator_queue_snapshot.write_snapshot(snapshot, target)

    assert written == target
    assert not target.with_suffix(".json.tmp").exists()
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "clear"


def test_main_no_write_json_does_not_create_default(monkeypatch, capsys, tmp_path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "operator_queue_snapshot.json"
    monkeypatch.setattr(workspace_roots, "ETA_OPERATOR_QUEUE_SNAPSHOT_PATH", target)
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

    rc = operator_queue_snapshot.main(["--out", str(target), "--json", "--no-write"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "clear"
    assert not target.exists()


def test_main_strict_returns_two_when_blocked(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
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

    rc = operator_queue_snapshot.main(["--out", str(tmp_path / "snapshot.json"), "--strict"])

    assert rc == 2
