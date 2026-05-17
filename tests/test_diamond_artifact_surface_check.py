from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eta_engine.scripts import diamond_artifact_surface_check


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_canonical_fresh_with_missing_root_var_alias_is_healthy_by_default(tmp_path: Path) -> None:
    now = datetime(2026, 5, 15, 18, 0, tzinfo=UTC)
    state_root = tmp_path / "var" / "eta_engine" / "state"
    root_var_dir = tmp_path / "var"
    _write_json(
        state_root / "diamond_leaderboard_latest.json",
        {
            "ts": (now - timedelta(minutes=5)).isoformat(),
            "leaderboard": [],
        },
    )
    _write_json(
        state_root / "diamond_edge_audit_latest.json",
        {
            "generated_at_utc": (now - timedelta(minutes=10)).isoformat(),
            "retune_queue": [],
        },
    )
    _write_json(
        state_root / "diamond_ops_dashboard_latest.json",
        {
            "ts": (now - timedelta(minutes=15)).isoformat(),
            "syntheses": [],
        },
    )
    _write_json(
        state_root / "diamond_promotion_gate_latest.json",
        {
            "ts": (now - timedelta(hours=2)).isoformat(),
            "candidates": [],
        },
    )
    _write_json(
        state_root / "closed_trade_ledger_latest.json",
        {
            "generated_at_utc": (now - timedelta(minutes=20)).isoformat(),
            "closed_trade_count": 12,
        },
    )

    report = diamond_artifact_surface_check.build_diamond_artifact_surface_report(
        state_root=state_root,
        root_var_dir=root_var_dir,
        now=now,
    )

    assert report["healthy"] is True
    assert report["status"] == "fresh"
    assert report["diagnosis"] == "canonical_artifacts_fresh"
    assert report["warning_count"] == 0
    assert report["critical_count"] == 0
    assert all(artifact["diagnosis"] == "canonical_fresh_root_var_alias_not_required" for artifact in report["artifacts"])
    assert all(artifact["surface_status"] == "ok" for artifact in report["artifacts"])


def test_compatibility_mode_keeps_missing_root_var_alias_visible(tmp_path: Path) -> None:
    now = datetime(2026, 5, 15, 18, 0, tzinfo=UTC)
    state_root = tmp_path / "var" / "eta_engine" / "state"
    root_var_dir = tmp_path / "var"
    for filename in diamond_artifact_surface_check.FRESHNESS_LIMITS_HOURS:
        _write_json(state_root / filename, {"ts": (now - timedelta(minutes=5)).isoformat()})

    report = diamond_artifact_surface_check.build_diamond_artifact_surface_report(
        state_root=state_root,
        root_var_dir=root_var_dir,
        now=now,
        warn_on_missing_root_var_alias=True,
    )

    assert report["healthy"] is True
    assert report["status"] == "surface_warning"
    assert report["diagnosis"] == "canonical_ready_root_var_missing"
    assert report["warning_count"] == len(diamond_artifact_surface_check.FRESHNESS_LIMITS_HOURS)
    assert report["critical_count"] == 0
    assert all(artifact["diagnosis"] == "canonical_ready_root_var_missing" for artifact in report["artifacts"])
    assert all(artifact["surface_status"] == "warning" for artifact in report["artifacts"])


def test_fresh_root_var_alias_without_fresh_canonical_is_critical(tmp_path: Path) -> None:
    now = datetime(2026, 5, 15, 18, 0, tzinfo=UTC)
    state_root = tmp_path / "var" / "eta_engine" / "state"
    root_var_dir = tmp_path / "var"
    _write_json(
        state_root / "diamond_leaderboard_latest.json",
        {
            "ts": (now - timedelta(hours=3)).isoformat(),
            "leaderboard": [],
        },
    )
    _write_json(
        root_var_dir / "diamond_leaderboard_latest.json",
        {
            "ts": (now - timedelta(minutes=5)).isoformat(),
            "leaderboard": [],
        },
    )

    report = diamond_artifact_surface_check.build_diamond_artifact_surface_report(
        state_root=state_root,
        root_var_dir=root_var_dir,
        now=now,
    )

    leaderboard = next(
        artifact for artifact in report["artifacts"] if artifact["filename"] == "diamond_leaderboard_latest.json"
    )
    assert report["healthy"] is False
    assert report["status"] == "critical"
    assert report["diagnosis"] == "canonical_missing_or_stale_root_var_alias_only"
    assert leaderboard["healthy"] is False
    assert leaderboard["status"] == "stale"
    assert leaderboard["surface_status"] == "critical"
    assert leaderboard["diagnosis"] == "canonical_stale_root_var_alias_only"


def test_write_report_uses_canonical_health_dir(tmp_path: Path) -> None:
    now = datetime(2026, 5, 15, 18, 0, tzinfo=UTC)
    state_root = tmp_path / "var" / "eta_engine" / "state"
    root_var_dir = tmp_path / "var"
    for filename in diamond_artifact_surface_check.FRESHNESS_LIMITS_HOURS:
        _write_json(state_root / filename, {"ts": now.isoformat()})
        _write_json(root_var_dir / filename, {"ts": now.isoformat()})

    report = diamond_artifact_surface_check.build_diamond_artifact_surface_report(
        state_root=state_root,
        root_var_dir=root_var_dir,
        now=now,
    )
    report_path = diamond_artifact_surface_check.write_diamond_artifact_surface_report(
        report,
        state_root=state_root,
    )

    assert report["healthy"] is True
    assert report["status"] == "fresh"
    assert report_path == state_root / "health" / "diamond_artifact_surface_check_latest.json"
    assert json.loads(report_path.read_text(encoding="utf-8"))["diagnosis"] == "canonical_artifacts_fresh"
