from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy" / "scripts" / "run_broker_state_refresh_task.cmd"
REGISTRAR = ROOT / "deploy" / "scripts" / "register_broker_state_refresh_task.ps1"
BOOTSTRAP = ROOT / "deploy" / "vps_bootstrap.ps1"
RUNBOOK = ROOT / "docs" / "VPS_OPS_HARDENING_RUNBOOK.md"
REPAIR = ROOT / "deploy" / "scripts" / "repair_dashboard_durability_admin.cmd"


def test_broker_state_refresh_runner_warms_read_only_broker_cache() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert r"ETA_ROOT=C:\EvolutionaryTradingAlgo" in text
    assert r"ETA_STATE_DIR=%ETA_ROOT%\var\eta_engine\state" in text
    assert r"ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine" in text
    assert "-m eta_engine.scripts.broker_state_refresh_heartbeat" in text
    assert '--out "%ETA_STATE_DIR%\\broker_state_refresh_heartbeat.json"' in text
    assert "broker_state_refresh_heartbeat.task.log" in text
    assert "exit /b %REFRESH_RC%" in text
    assert "places, cancels, flattens, or promotes" in text


def test_broker_state_refresh_registrar_is_canonical_read_only_and_low_overhead() -> None:
    text = REGISTRAR.read_text(encoding="utf-8")

    assert "ETA-BrokerStateRefreshHeartbeat" in text
    assert '$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert "Assert-CanonicalEtaPath" in text
    assert "run_broker_state_refresh_task.cmd" in text
    assert "broker_state_refresh_heartbeat.json" in text
    assert "-RepetitionInterval (New-TimeSpan -Minutes 5)" in text
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 2)" in text
    assert '-UserId "NT AUTHORITY\\SYSTEM"' in text
    assert "[switch]$CurrentUser" in text
    assert "-LogonType Interactive -RunLevel Limited" in text
    assert "never submits, cancels, flattens, or promotes" in text


def test_broker_state_refresh_is_wired_into_bootstrap_repair_and_runbook() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    repair = REPAIR.read_text(encoding="utf-8")

    assert "register_broker_state_refresh_task.ps1" in bootstrap
    assert "ETA-BrokerStateRefreshHeartbeat" in bootstrap
    assert "every 5m read-only broker cache refresh" in bootstrap
    assert "register_broker_state_refresh_task.ps1 -Start" in runbook
    assert "broker_state_refresh_heartbeat.json" in runbook
    assert "REGISTER_BROKER_STATE_REFRESH" in repair
    assert 'File "%REGISTER_BROKER_STATE_REFRESH%" -DryRun' in repair
    assert 'File "%REGISTER_BROKER_STATE_REFRESH%" -Start' in repair


def test_broker_state_refresh_task_scripts_do_not_use_legacy_write_paths() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in (RUNNER, REGISTRAR))

    assert "OneDrive" not in combined
    assert "LOCALAPPDATA" not in combined
    assert "C:\\mnq_data" not in combined
    assert "C:\\crypto_data" not in combined
    assert "TheFirm" not in combined
    assert "The_Firm" not in combined
