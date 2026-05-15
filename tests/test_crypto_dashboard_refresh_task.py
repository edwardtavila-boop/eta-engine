from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy" / "scripts" / "run_crypto_dashboard_refresh_task.cmd"
REGISTRAR = ROOT / "deploy" / "scripts" / "register_crypto_dashboard_refresh_task.ps1"
BOOTSTRAP = ROOT / "deploy" / "vps_bootstrap.ps1"
RUNBOOK = ROOT / "docs" / "VPS_OPS_HARDENING_RUNBOOK.md"
REPAIR = ROOT / "deploy" / "scripts" / "repair_dashboard_durability_admin.cmd"


def test_crypto_dashboard_refresh_runner_is_canonical_and_persists_latest_json() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert r"ETA_ROOT=C:\EvolutionaryTradingAlgo" in text
    assert r"ETA_STATE_DIR=%ETA_ROOT%\var\eta_engine\state" in text
    assert r"ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine" in text
    assert r'"%PYTHON_EXE%" "%ETA_ROOT%\scripts\refresh_crypto_dashboard_bars.py" --json' in text
    assert r'LATEST_JSON=%ETA_STATE_DIR%\crypto_dashboard_refresh_latest.json' in text
    assert 'copy /y "%STDOUT_TMP%" "%LATEST_JSON%" >nul' in text
    assert "crypto_dashboard_refresh.stdout.log" in text
    assert "crypto_dashboard_refresh.stderr.log" in text
    assert "crypto_dashboard_refresh.task.log" in text
    assert "Coinbase" in text
    assert "exit /b %REFRESH_RC%" in text


def test_crypto_dashboard_refresh_registrar_is_canonical_and_has_safe_fallback() -> None:
    text = REGISTRAR.read_text(encoding="utf-8")

    assert 'TaskName = "ETA-Crypto-Dashboard-Refresh"' in text
    assert '$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert "run_crypto_dashboard_refresh_task.cmd" in text
    assert "crypto_dashboard_refresh_latest.json" in text
    assert "IntervalMinutes -lt 1 -or $IntervalMinutes -gt 30" in text
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)" in text
    assert "MultipleInstances IgnoreNew" in text
    assert "RestartCount 3" in text
    assert 'ExecutionTimeLimit (New-TimeSpan -Minutes 2)' in text
    assert 'UserId "NT AUTHORITY\\SYSTEM"' in text
    assert "LogonType Interactive" in text
    assert "current_user:$currentUser" in text
    assert "Coinbase public candles" in text
    assert "never submits, cancels, flattens, or promotes" in text


def test_crypto_dashboard_refresh_is_wired_into_bootstrap_repair_and_runbook() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    repair = REPAIR.read_text(encoding="utf-8")

    assert "register_crypto_dashboard_refresh_task.ps1" in bootstrap
    assert "ETA-Crypto-Dashboard-Refresh" in bootstrap
    assert "ETA-Crypto-Dashboard-Refresh task (every 5m, BTC/ETH/SOL)" in bootstrap
    assert "register_crypto_dashboard_refresh_task.ps1 -Start" in runbook
    assert "crypto_dashboard_refresh_latest.json" in runbook
    assert "REGISTER_CRYPTO_REFRESH" in repair
    assert 'File "%REGISTER_CRYPTO_REFRESH%" -DryRun' in repair
    assert 'File "%REGISTER_CRYPTO_REFRESH%" -Start' in repair


def test_crypto_dashboard_refresh_scripts_avoid_legacy_paths() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in (RUNNER, REGISTRAR))

    assert "OneDrive" not in combined
    assert "LOCALAPPDATA" not in combined
    assert "C:\\mnq_data" not in combined
    assert "C:\\crypto_data" not in combined
    assert "TheFirm" not in combined
    assert "The_Firm" not in combined
