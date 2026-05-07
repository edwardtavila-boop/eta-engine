from __future__ import annotations

import json

from eta_engine.scripts import health_check


def _healthy_component(name: str):
    def _component() -> health_check.HealthComponent:
        return health_check.HealthComponent(name=name, healthy=True, status="healthy", detail="ok", score=1.0)

    return _component


def test_main_honors_output_dir_cli_and_writes_report(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(health_check, "_check_disk_space", _healthy_component("disk_space"))
    monkeypatch.setattr(health_check, "_check_kaizen_state", _healthy_component("kaizen_engine"))
    monkeypatch.setattr(health_check, "_check_quantum_freshness", _healthy_component("quantum_rebalance"))
    monkeypatch.setattr(health_check, "_check_hermes_connectivity", _healthy_component("hermes_bridge"))
    monkeypatch.setattr(health_check, "_check_supervisor_heartbeat", _healthy_component("supervisor_heartbeat"))
    monkeypatch.setattr(health_check, "_check_repo_health", _healthy_component("repo_health"))

    exit_code = health_check.main(["--output-dir", str(tmp_path)])

    assert exit_code == 0
    current_report = tmp_path / "current_health.json"
    assert current_report.exists()
    payload = json.loads(current_report.read_text(encoding="utf-8"))
    assert payload["overall_status"] == "healthy"
    assert any(path.name.startswith("health_check_") for path in tmp_path.iterdir())

    stdout = capsys.readouterr().out
    assert '"overall_status": "healthy"' in stdout
