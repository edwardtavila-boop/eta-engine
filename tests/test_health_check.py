from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eta_engine.scripts import health_check


def _healthy_component(name: str):
    def _component(*_args, **_kwargs) -> health_check.HealthComponent:
        return health_check.HealthComponent(name=name, healthy=True, status="healthy", detail="ok", score=1.0)

    return _component


def _missing_supervisor_component() -> health_check.HealthComponent:
    return health_check.HealthComponent(
        name="supervisor_heartbeat",
        healthy=False,
        status="missing",
        detail="canonical_heartbeat_missing; canonical age unknown",
        score=0.1,
    )


def _write_quantum_allocation(path: Path, ts: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ts": ts.isoformat(), "results": []}), encoding="utf-8")


def _patch_baseline_components(monkeypatch) -> None:
    monkeypatch.setattr(health_check, "_check_disk_space", _healthy_component("disk_space"))
    monkeypatch.setattr(health_check, "_check_kaizen_state", _healthy_component("kaizen_engine"))
    monkeypatch.setattr(health_check, "_check_quantum_freshness", _healthy_component("quantum_rebalance"))
    monkeypatch.setattr(health_check, "_check_hermes_connectivity", _healthy_component("hermes_bridge"))
    monkeypatch.setattr(health_check, "_check_diamond_artifact_surface", _healthy_component("diamond_artifact_surface"))
    monkeypatch.setattr(health_check, "_check_diamond_retune_truth", _healthy_component("diamond_retune_truth"))
    monkeypatch.setattr(health_check, "_check_repo_health", _healthy_component("repo_health"))


def test_default_output_dir_is_canonical_runtime_health_dir() -> None:
    from eta_engine.scripts import workspace_roots

    assert health_check.DEFAULT_OUTPUT_DIR == workspace_roots.ETA_RUNTIME_STATE_DIR / "health"


def test_kaizen_health_prefers_active_loop_latest_json(tmp_path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    reports_dir = state_dir / "kaizen_reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "kaizen_20260508T150144Z.json").write_text("{}", encoding="utf-8")
    (state_dir / "kaizen_latest.json").write_text(
        json.dumps(
            {
                "started_at": datetime.now(UTC).isoformat(),
                "applied": True,
                "n_bots": 2,
                "applied_count": 1,
                "held_count": 1,
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(health_check, "_STATE_DIR", state_dir)

    component = health_check._check_kaizen_state()

    assert component.healthy is True
    assert component.status == "healthy"
    assert "active loop latest" in component.detail
    assert "applied_count=1" in component.detail
    assert "reports=1" in component.detail


def test_quantum_health_prefers_canonical_current_allocation(tmp_path, monkeypatch) -> None:
    current = tmp_path / "var" / "eta_engine" / "state" / "quantum" / "current_allocation.json"
    legacy_current = tmp_path / "eta_engine" / "state" / "quantum" / "current_allocation.json"
    _write_quantum_allocation(current, datetime.now(UTC) - timedelta(minutes=15))
    monkeypatch.setattr(health_check, "ETA_QUANTUM_CURRENT_ALLOCATION_PATH", current)
    monkeypatch.setattr(health_check, "ETA_QUANTUM_STATE_DIR", current.parent)
    monkeypatch.setattr(health_check, "ETA_LEGACY_QUANTUM_CURRENT_ALLOCATION_PATH", legacy_current)
    monkeypatch.setattr(health_check, "ETA_LEGACY_QUANTUM_STATE_DIR", legacy_current.parent)

    component = health_check._check_quantum_freshness()

    assert component.healthy is True
    assert component.status == "healthy"
    assert component.score == 1.0
    assert "last rebalance" in component.detail


def test_quantum_health_uses_legacy_allocation_as_migration_fallback(tmp_path, monkeypatch) -> None:
    current = tmp_path / "var" / "eta_engine" / "state" / "quantum" / "current_allocation.json"
    legacy_current = tmp_path / "eta_engine" / "state" / "quantum" / "current_allocation.json"
    _write_quantum_allocation(legacy_current, datetime.now(UTC) - timedelta(minutes=20))
    monkeypatch.setattr(health_check, "ETA_QUANTUM_CURRENT_ALLOCATION_PATH", current)
    monkeypatch.setattr(health_check, "ETA_QUANTUM_STATE_DIR", current.parent)
    monkeypatch.setattr(health_check, "ETA_LEGACY_QUANTUM_CURRENT_ALLOCATION_PATH", legacy_current)
    monkeypatch.setattr(health_check, "ETA_LEGACY_QUANTUM_STATE_DIR", legacy_current.parent)

    component = health_check._check_quantum_freshness()

    assert component.healthy is True
    assert component.status == "legacy_migration"
    assert component.score == 0.8
    assert "legacy allocation fallback" in component.detail


def test_main_honors_output_dir_cli_and_writes_report(tmp_path, monkeypatch, capsys) -> None:
    _patch_baseline_components(monkeypatch)
    monkeypatch.setattr(health_check, "_check_supervisor_heartbeat", _healthy_component("supervisor_heartbeat"))

    exit_code = health_check.main(["--output-dir", str(tmp_path)])

    assert exit_code == 0
    current_report = tmp_path / "current_health.json"
    assert current_report.exists()
    payload = json.loads(current_report.read_text(encoding="utf-8"))
    assert payload["overall_status"] == "healthy"
    assert any(path.name.startswith("health_check_") for path in tmp_path.iterdir())

    stdout = capsys.readouterr().out
    assert '"overall_status": "healthy"' in stdout


def test_remote_supervisor_truth_suppresses_local_heartbeat_action_item(monkeypatch) -> None:
    _patch_baseline_components(monkeypatch)
    monkeypatch.setattr(health_check, "_check_supervisor_heartbeat", _missing_supervisor_component)

    strict = health_check.run_health_check(output_dir=None)
    assert any("supervisor_heartbeat" in item for item in strict.action_items)

    report = health_check.run_health_check(output_dir=None, allow_remote_supervisor_truth=True)

    supervisor = next(component for component in report.components if component.name == "supervisor_heartbeat")
    assert supervisor.healthy is True
    assert supervisor.status == "remote_supervisor_truth"
    assert report.action_items == []


def test_remote_retune_truth_suppresses_local_mismatch_action_item(monkeypatch) -> None:
    monkeypatch.setattr(health_check, "_check_disk_space", _healthy_component("disk_space"))
    monkeypatch.setattr(health_check, "_check_kaizen_state", _healthy_component("kaizen_engine"))
    monkeypatch.setattr(health_check, "_check_quantum_freshness", _healthy_component("quantum_rebalance"))
    monkeypatch.setattr(health_check, "_check_hermes_connectivity", _healthy_component("hermes_bridge"))
    monkeypatch.setattr(health_check, "_check_supervisor_heartbeat", _healthy_component("supervisor_heartbeat"))
    monkeypatch.setattr(health_check, "_check_diamond_artifact_surface", _healthy_component("diamond_artifact_surface"))
    monkeypatch.setattr(health_check, "_check_repo_health", _healthy_component("repo_health"))
    monkeypatch.setattr(
        health_check,
        "build_diamond_retune_truth_report",
        lambda state_root: {
            "healthy": False,
            "status": "warning",
            "diagnosis": "public_local_focus_mismatch",
            "mismatch_count": 9,
            "action_items": ["Refresh or repair the local closed-trade ledger and diamond_retune_status writers."],
            "public_surface": {
                "available": True,
                "readable": True,
            },
        },
    )

    strict = health_check.run_health_check(output_dir=None)
    assert any("diamond_retune_truth" in item for item in strict.action_items)

    report = health_check.run_health_check(output_dir=None, allow_remote_retune_truth=True)

    retune = next(component for component in report.components if component.name == "diamond_retune_truth")
    assert retune.healthy is True
    assert retune.status == "remote_retune_truth"
    assert report.action_items == []


def test_diamond_artifact_surface_warning_stays_healthy_but_visible(monkeypatch) -> None:
    monkeypatch.setattr(
        health_check,
        "build_diamond_artifact_surface_report",
        lambda state_root: {
            "healthy": True,
            "status": "surface_warning",
            "diagnosis": "canonical_ready_root_var_missing",
            "warning_count": 2,
            "critical_count": 0,
            "action_items": ["Update local watch surfaces to read var/eta_engine/state first."],
        },
    )

    component = health_check._check_diamond_artifact_surface()

    assert component.name == "diamond_artifact_surface"
    assert component.healthy is True
    assert component.status == "warning"
    assert component.score == 0.8
    assert "canonical_ready_root_var_missing" in component.detail
    assert "action: Update local watch surfaces to read var/eta_engine/state first." in component.detail


def test_diamond_artifact_surface_critical_adds_action_item(monkeypatch) -> None:
    monkeypatch.setattr(
        health_check,
        "build_diamond_artifact_surface_report",
        lambda state_root: {
            "healthy": False,
            "status": "critical",
            "diagnosis": "canonical_artifacts_unhealthy",
            "warning_count": 1,
            "critical_count": 4,
            "action_items": ["Refresh or repair closed_trade_ledger_latest.json."],
        },
    )
    monkeypatch.setattr(health_check, "_check_disk_space", _healthy_component("disk_space"))
    monkeypatch.setattr(health_check, "_check_kaizen_state", _healthy_component("kaizen_engine"))
    monkeypatch.setattr(health_check, "_check_quantum_freshness", _healthy_component("quantum_rebalance"))
    monkeypatch.setattr(health_check, "_check_hermes_connectivity", _healthy_component("hermes_bridge"))
    monkeypatch.setattr(health_check, "_check_supervisor_heartbeat", _healthy_component("supervisor_heartbeat"))
    monkeypatch.setattr(health_check, "_check_repo_health", _healthy_component("repo_health"))

    component = health_check._check_diamond_artifact_surface()
    report = health_check.run_health_check(output_dir=None)

    assert component.name == "diamond_artifact_surface"
    assert component.healthy is False
    assert component.status == "critical"
    assert component.score == 0.2
    assert report.overall_status == "warning"
    assert report.exit_code == 1
    assert any("diamond_artifact_surface" in item for item in report.action_items)


def test_diamond_retune_truth_warning_adds_action_item(monkeypatch) -> None:
    monkeypatch.setattr(
        health_check,
        "build_diamond_retune_truth_report",
        lambda state_root: {
            "healthy": False,
            "status": "warning",
            "diagnosis": "public_local_focus_mismatch",
            "mismatch_count": 6,
            "action_items": ["Refresh or repair the local closed-trade ledger and diamond_retune_status writers."],
        },
    )
    monkeypatch.setattr(health_check, "_check_disk_space", _healthy_component("disk_space"))
    monkeypatch.setattr(health_check, "_check_kaizen_state", _healthy_component("kaizen_engine"))
    monkeypatch.setattr(health_check, "_check_quantum_freshness", _healthy_component("quantum_rebalance"))
    monkeypatch.setattr(health_check, "_check_hermes_connectivity", _healthy_component("hermes_bridge"))
    monkeypatch.setattr(health_check, "_check_supervisor_heartbeat", _healthy_component("supervisor_heartbeat"))
    monkeypatch.setattr(health_check, "_check_diamond_artifact_surface", _healthy_component("diamond_artifact_surface"))
    monkeypatch.setattr(health_check, "_check_repo_health", _healthy_component("repo_health"))

    component = health_check._check_diamond_retune_truth()
    report = health_check.run_health_check(output_dir=None)

    assert component.name == "diamond_retune_truth"
    assert component.healthy is False
    assert component.status == "warning"
    assert component.score == 0.6
    assert "public_local_focus_mismatch" in component.detail
    assert report.overall_status == "healthy"
    assert report.exit_code == 0
    assert any("diamond_retune_truth" in item for item in report.action_items)


def test_diamond_retune_truth_provenance_gap_surfaces_public_vs_canonical_counts(monkeypatch) -> None:
    monkeypatch.setattr(
        health_check,
        "build_diamond_retune_truth_report",
        lambda state_root: {
            "healthy": False,
            "status": "warning",
            "diagnosis": "public_focus_provenance_gap",
            "mismatch_count": 0,
            "warnings": [
                "Public broker-backed close sample materially exceeds the local canonical trade_closes sample "
                "for mnq_futures_sage (141 vs 5).",
            ],
            "action_items": [
                "Refresh or repair the canonical trade_closes writer at "
                "C:/EvolutionaryTradingAlgo/var/eta_engine/state/jarvis_intel/trade_closes.jsonl "
                "from the authoritative VPS/public close source before trusting local broker-proof counts.",
            ],
            "public_focus_provenance_gap": {
                "status": "material_gap",
                "public_focus_closed_trade_count": 141,
                "canonical_bot_row_count": 5,
                "legacy_bot_row_count": 1267,
                "warning": (
                    "Public broker-backed close sample materially exceeds the local canonical trade_closes "
                    "sample for mnq_futures_sage (141 vs 5)."
                ),
                "action": (
                    "Refresh or repair the canonical trade_closes writer at "
                    "C:/EvolutionaryTradingAlgo/var/eta_engine/state/jarvis_intel/trade_closes.jsonl "
                    "from the authoritative VPS/public close source before trusting local broker-proof counts."
                ),
            },
            "public_surface": {
                "available": True,
                "readable": True,
            },
        },
    )
    monkeypatch.setattr(health_check, "_check_disk_space", _healthy_component("disk_space"))
    monkeypatch.setattr(health_check, "_check_kaizen_state", _healthy_component("kaizen_engine"))
    monkeypatch.setattr(health_check, "_check_quantum_freshness", _healthy_component("quantum_rebalance"))
    monkeypatch.setattr(health_check, "_check_hermes_connectivity", _healthy_component("hermes_bridge"))
    monkeypatch.setattr(health_check, "_check_supervisor_heartbeat", _healthy_component("supervisor_heartbeat"))
    monkeypatch.setattr(health_check, "_check_diamond_artifact_surface", _healthy_component("diamond_artifact_surface"))
    monkeypatch.setattr(health_check, "_check_repo_health", _healthy_component("repo_health"))

    component = health_check._check_diamond_retune_truth()
    report = health_check.run_health_check(output_dir=None, allow_remote_retune_truth=True)

    assert component.name == "diamond_retune_truth"
    assert component.healthy is False
    assert component.status == "warning"
    assert "provenance: public_closes=141 canonical_rows=5 legacy_rows=1267" in component.detail
    assert "materially exceeds the local canonical trade_closes sample" in component.detail
    retune = next(item for item in report.components if item.name == "diamond_retune_truth")
    assert retune.healthy is True
    assert retune.status == "remote_retune_truth"
    assert report.action_items == []


def test_diamond_retune_truth_persists_latest_report(monkeypatch) -> None:
    captured: dict[str, object] = {}
    report_payload = {
        "healthy": True,
        "status": "healthy",
        "diagnosis": "public_local_focus_match",
        "mismatch_count": 0,
        "public_surface": {
            "available": True,
            "readable": True,
        },
    }

    monkeypatch.setattr(health_check, "build_diamond_retune_truth_report", lambda state_root: report_payload)
    monkeypatch.setattr(
        health_check,
        "write_diamond_retune_truth_report",
        lambda report: captured.setdefault("report", report),
    )
    monkeypatch.setattr(health_check, "write_public_retune_truth_cache", lambda surface: None)
    monkeypatch.setattr(health_check, "write_public_broker_close_truth_cache", lambda surface: None)

    component = health_check._check_diamond_retune_truth()

    assert component.name == "diamond_retune_truth"
    assert component.healthy is True
    assert captured["report"] == report_payload
