from __future__ import annotations

import json
from datetime import UTC, datetime

from eta_engine.scripts import health_check


def _healthy_component(name: str):
    def _component() -> health_check.HealthComponent:
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


def _patch_baseline_components(monkeypatch) -> None:
    monkeypatch.setattr(health_check, "_check_disk_space", _healthy_component("disk_space"))
    monkeypatch.setattr(health_check, "_check_kaizen_state", _healthy_component("kaizen_engine"))
    monkeypatch.setattr(health_check, "_check_quantum_freshness", _healthy_component("quantum_rebalance"))
    monkeypatch.setattr(health_check, "_check_hermes_connectivity", _healthy_component("hermes_bridge"))
    monkeypatch.setattr(health_check, "_check_repo_health", _healthy_component("repo_health"))


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
