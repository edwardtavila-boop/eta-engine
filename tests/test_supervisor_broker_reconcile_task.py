from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy" / "scripts" / "run_supervisor_broker_reconcile_task.cmd"
REGISTRAR = ROOT / "deploy" / "scripts" / "register_supervisor_broker_reconcile_task.ps1"
BOOTSTRAP = ROOT / "deploy" / "vps_bootstrap.ps1"
RUNBOOK = ROOT / "docs" / "VPS_OPS_HARDENING_RUNBOOK.md"
REPAIR = ROOT / "deploy" / "scripts" / "repair_dashboard_durability_admin.cmd"
AUDIT = ROOT / "scripts" / "vps_ops_hardening_audit.py"


def test_supervisor_broker_reconcile_runner_refreshes_read_only_artifacts() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert r"ETA_ROOT=C:\EvolutionaryTradingAlgo" in text
    assert r"ETA_STATE_DIR=%ETA_ROOT%\var\eta_engine\state" in text
    assert "-m eta_engine.scripts.supervisor_broker_reconcile_heartbeat" in text
    assert '--out "%ETA_STATE_DIR%\\jarvis_intel\\supervisor\\reconcile_last.json"' in text
    assert '--status-out "%ETA_STATE_DIR%\\supervisor_broker_reconcile_heartbeat.json"' in text
    assert "supervisor_broker_reconcile_heartbeat.task.log" in text
    assert "places, cancels, flattens, acknowledges, or promotes" in text


def test_supervisor_broker_reconcile_registrar_is_canonical_read_only() -> None:
    text = REGISTRAR.read_text(encoding="utf-8")

    assert "ETA-SupervisorBrokerReconcile" in text
    assert '$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert "Assert-CanonicalEtaPath" in text
    assert "run_supervisor_broker_reconcile_task.cmd" in text
    assert "reconcile_last.json" in text
    assert "supervisor_broker_reconcile_heartbeat.json" in text
    assert "-RepetitionInterval (New-TimeSpan -Minutes 5)" in text
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 2)" in text
    assert '-UserId "NT AUTHORITY\\SYSTEM"' in text
    assert "[switch]$CurrentUser" in text
    assert "never submits, cancels, flattens, acknowledges, or promotes" in text


def test_supervisor_broker_reconcile_is_wired_into_vps_surfaces() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    repair = REPAIR.read_text(encoding="utf-8")
    audit = AUDIT.read_text(encoding="utf-8")

    assert "register_supervisor_broker_reconcile_task.ps1" in bootstrap
    assert "ETA-SupervisorBrokerReconcile" in bootstrap
    assert "every 5m read-only broker/supervisor reconcile" in bootstrap
    assert "register_supervisor_broker_reconcile_task.ps1 -Start" in runbook
    assert "supervisor_broker_reconcile_heartbeat.json" in runbook
    assert "REGISTER_SUPERVISOR_BROKER_RECONCILE" in repair
    assert 'File "%REGISTER_SUPERVISOR_BROKER_RECONCILE%" -DryRun' in repair
    assert 'File "%REGISTER_SUPERVISOR_BROKER_RECONCILE%" -Start' in repair
    assert '"ETA-SupervisorBrokerReconcile"' in audit


def test_supervisor_broker_reconcile_task_scripts_do_not_use_legacy_write_paths() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in (RUNNER, REGISTRAR))

    assert "OneDrive" not in combined
    assert "LOCALAPPDATA" not in combined
    assert "C:\\mnq_data" not in combined
    assert "C:\\crypto_data" not in combined
    assert "TheFirm" not in combined
    assert "The_Firm" not in combined
