from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts import health_check, supervisor_heartbeat_check


def _write_heartbeat(path: Path, ts: datetime, *, tick_count: int = 7) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ts": ts.isoformat(),
                "tick_count": tick_count,
                "mode": "paper_live",
                "feed": "composite",
                "feed_health": "ok",
                "bots": [{"bot_id": "mnq-alpha"}],
            }
        ),
        encoding="utf-8",
    )


def test_canonical_fresh_explains_legacy_path_mismatch(tmp_path: Path) -> None:
    now = datetime(2026, 5, 5, 6, 20, tzinfo=UTC)
    state_root = tmp_path / "var" / "eta_engine" / "state"
    eta_root = tmp_path / "eta_engine"
    _write_heartbeat(state_root / "jarvis_intel" / "supervisor" / "heartbeat.json", now - timedelta(seconds=15))

    report = supervisor_heartbeat_check.build_supervisor_heartbeat_report(
        state_root=state_root,
        eta_engine_root=eta_root,
        now=now,
        threshold_minutes=10,
    )

    assert report["healthy"] is True
    assert report["status"] == "fresh"
    assert report["diagnosis"] == "canonical_fresh_legacy_path_mismatch"
    assert report["canonical_age_seconds"] == 15.0
    assert report["warnings"]
    assert report["candidates"][0]["payload_summary"]["bot_count"] == 1


def test_fresh_mirror_without_fresh_canonical_is_wrong_write_path(tmp_path: Path) -> None:
    now = datetime(2026, 5, 5, 6, 20, tzinfo=UTC)
    state_root = tmp_path / "var" / "eta_engine" / "state"
    eta_root = tmp_path / "eta_engine"
    _write_heartbeat(state_root / "jarvis_intel" / "supervisor" / "heartbeat.json", now - timedelta(minutes=20))
    _write_heartbeat(eta_root / "state" / "jarvis_intel" / "supervisor" / "heartbeat.json", now - timedelta(seconds=10))

    report = supervisor_heartbeat_check.build_supervisor_heartbeat_report(
        state_root=state_root,
        eta_engine_root=eta_root,
        now=now,
        threshold_minutes=10,
    )

    assert report["healthy"] is False
    assert report["status"] == "wrong_write_path"
    assert report["diagnosis"] == "canonical_stale_eta_engine_state_mirror_fresh"
    assert report["latest_label"] == "eta_engine_state_mirror"
    assert report["action_items"]


def test_write_report_uses_canonical_health_dir(tmp_path: Path) -> None:
    now = datetime(2026, 5, 5, 6, 20, tzinfo=UTC)
    state_root = tmp_path / "var" / "eta_engine" / "state"
    eta_root = tmp_path / "eta_engine"
    _write_heartbeat(state_root / "jarvis_intel" / "supervisor" / "heartbeat.json", now)
    report = supervisor_heartbeat_check.build_supervisor_heartbeat_report(
        state_root=state_root,
        eta_engine_root=eta_root,
        now=now,
    )

    report_path = supervisor_heartbeat_check.write_supervisor_heartbeat_report(report, state_root=state_root)

    assert report_path == state_root / "health" / "supervisor_heartbeat_check_latest.json"
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "fresh"


def test_health_check_surfaces_supervisor_component(monkeypatch) -> None:
    monkeypatch.setattr(
        health_check,
        "build_supervisor_heartbeat_report",
        lambda state_root: {
            "healthy": True,
            "status": "fresh",
            "diagnosis": "canonical_fresh_legacy_path_mismatch",
            "canonical_age_seconds": 12.5,
        },
    )

    component = health_check._check_supervisor_heartbeat()

    assert component.name == "supervisor_heartbeat"
    assert component.healthy is True
    assert component.status == "healthy"
    assert "canonical_fresh_legacy_path_mismatch" in component.detail
