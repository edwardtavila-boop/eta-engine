from __future__ import annotations

import json

from eta_engine.scripts import flaw_hardening_heartbeat


def _scorecard(*, status: str = "limited", blocker: str = "R8_BROKER_TRUTH_CONFIRMED") -> dict[str, object]:
    return {
        "status": status,
        "composite_score": 4.0,
        "grade": "E",
        "summary": {
            "launch_readiness_primary_blocker": blocker,
            "launch_readiness_primary_blocker_detail": "negative broker sample",
            "composite_score_cap_reason": "broker truth failing",
        },
    }


def _prop_gate(*, summary: str = "BLOCKED", blocker: str = "prop_readiness") -> dict[str, object]:
    return {
        "summary": summary,
        "checks": [
            {"name": blocker, "status": "BLOCKED", "detail": "missing secrets"},
        ],
        "next_actions": ["stage secrets"],
    }


def _launch(*, verdict: str = "NO_GO", blocker: str = "R8_BROKER_TRUTH_CONFIRMED") -> dict[str, object]:
    return {
        "overall_verdict": verdict,
        "gates": [
            {"name": blocker, "status": verdict, "rationale": "negative broker sample"},
        ],
    }


def test_build_snapshot_surfaces_core_flaw_truth(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    for name, lines in (
        ("jarvis_strategy_supervisor", 100),
        ("broker_router", 50),
        ("dashboard_api", 200),
    ):
        path = tmp_path / f"{name}.py"
        path.write_text("\n".join("x" for _ in range(lines)) + "\n", encoding="utf-8")
        monkeypatch.setitem(flaw_hardening_heartbeat._HOTSPOT_PATHS, name, path)

    snapshot = flaw_hardening_heartbeat.build_snapshot(
        scorecard=_scorecard(),
        prop_live_readiness=_prop_gate(),
        launch_readiness=_launch(),
    )

    assert snapshot["status"] == "blocked"
    assert snapshot["scorecard_primary_blocker"] == "R8_BROKER_TRUTH_CONFIRMED"
    assert snapshot["prop_live_readiness_primary_blocker"] == "prop_readiness"
    assert snapshot["launch_readiness_primary_blocker"] == "R8_BROKER_TRUTH_CONFIRMED"
    assert snapshot["architecture_hotspot_max_name"] == "dashboard_api"
    assert snapshot["architecture_hotspot_max_lines"] == 200


def test_build_heartbeat_marks_notify_from_drift() -> None:
    heartbeat = flaw_hardening_heartbeat.build_heartbeat(
        {
            "generated_at": "2026-05-17T00:00:00+00:00",
            "status": "blocked",
            "scorecard_status": "limited",
            "scorecard_composite_score": 4.0,
            "scorecard_primary_blocker": "R8_BROKER_TRUTH_CONFIRMED",
            "prop_live_readiness_status": "BLOCKED",
            "prop_live_readiness_primary_blocker": "prop_readiness",
            "launch_readiness_verdict": "NO_GO",
            "launch_readiness_primary_blocker": "R8_BROKER_TRUTH_CONFIRMED",
            "architecture_hotspot_max_name": "dashboard_api",
            "architecture_hotspot_max_lines": 17964,
            "drift": {
                "changed": True,
                "summary": "flaw hardening drift detected: scorecard_status",
                "changed_fields": ["scorecard_status"],
            },
        },
        None,
    )

    assert heartbeat["notify"] is True
    assert heartbeat["changed_fields"] == ["scorecard_status"]
    assert heartbeat["architecture_hotspot_max_name"] == "dashboard_api"


def test_main_changed_only_suppresses_unchanged_output(monkeypatch, capsys, tmp_path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "flaw_hardening_snapshot.json"
    target.write_text(
        json.dumps(
            {
                "status": "blocked",
                "scorecard_status": "limited",
                "scorecard_composite_score": 4.0,
                "scorecard_primary_blocker": "R8_BROKER_TRUTH_CONFIRMED",
                "prop_live_readiness_status": "BLOCKED",
                "prop_live_readiness_primary_blocker": "prop_readiness",
                "launch_readiness_verdict": "NO_GO",
                "launch_readiness_primary_blocker": "R8_BROKER_TRUTH_CONFIRMED",
                "architecture_hotspot_max_name": "dashboard_api",
                "architecture_hotspot_max_lines": 17964,
                "architecture_hotspot_lines": {"dashboard_api": 17964},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        flaw_hardening_heartbeat,
        "build_snapshot",
        lambda: {
            "status": "blocked",
            "scorecard_status": "limited",
            "scorecard_composite_score": 4.0,
            "scorecard_primary_blocker": "R8_BROKER_TRUTH_CONFIRMED",
            "prop_live_readiness_status": "BLOCKED",
            "prop_live_readiness_primary_blocker": "prop_readiness",
            "launch_readiness_verdict": "NO_GO",
            "launch_readiness_primary_blocker": "R8_BROKER_TRUTH_CONFIRMED",
            "architecture_hotspot_max_name": "dashboard_api",
            "architecture_hotspot_max_lines": 17964,
            "architecture_hotspot_lines": {"dashboard_api": 17964},
            "generated_at": "2026-05-17T00:00:00+00:00",
        },
    )

    rc = flaw_hardening_heartbeat.main(["--out", str(target), "--changed-only"])

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_strict_blockers_returns_two(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        flaw_hardening_heartbeat,
        "build_snapshot",
        lambda: {
            "status": "blocked",
            "scorecard_status": "limited",
            "scorecard_composite_score": 4.0,
            "scorecard_primary_blocker": "R8_BROKER_TRUTH_CONFIRMED",
            "prop_live_readiness_status": "BLOCKED",
            "prop_live_readiness_primary_blocker": "prop_readiness",
            "launch_readiness_verdict": "NO_GO",
            "launch_readiness_primary_blocker": "R8_BROKER_TRUTH_CONFIRMED",
            "architecture_hotspot_max_name": "dashboard_api",
            "architecture_hotspot_max_lines": 17964,
            "architecture_hotspot_lines": {"dashboard_api": 17964},
            "generated_at": "2026-05-17T00:00:00+00:00",
        },
    )

    rc = flaw_hardening_heartbeat.main(["--out", str(tmp_path / "snapshot.json"), "--strict-blockers"])

    assert rc == 2
