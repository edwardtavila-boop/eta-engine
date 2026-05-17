from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy" / "scripts" / "run_flaw_hardening_heartbeat_task.cmd"
REGISTRAR = ROOT / "deploy" / "scripts" / "register_flaw_hardening_heartbeat_task.ps1"
BOOTSTRAP = ROOT / "deploy" / "vps_bootstrap.ps1"
RUNBOOK = ROOT / "docs" / "VPS_OPS_HARDENING_RUNBOOK.md"


def test_flaw_hardening_runner_refreshes_canonical_snapshot() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert r"ETA_ROOT=C:\EvolutionaryTradingAlgo" in text
    assert r"ETA_STATE_DIR=%ETA_ROOT%\var\eta_engine\state" in text
    assert r"ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine" in text
    assert "-m eta_engine.scripts.flaw_hardening_heartbeat" in text
    assert '--out "%ETA_STATE_DIR%\\flaw_hardening_snapshot.json"' in text
    assert '--previous "%ETA_STATE_DIR%\\flaw_hardening_snapshot.previous.json"' in text
    assert "--changed-only" in text
    assert "flaw_hardening_heartbeat.task.log" in text
    assert "exit /b %HEARTBEAT_RC%" in text


def test_flaw_hardening_registrar_is_canonical_read_only_and_low_overhead() -> None:
    text = REGISTRAR.read_text(encoding="utf-8")

    assert "ETA-FlawHardeningHeartbeat" in text
    assert '$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert "Assert-CanonicalEtaPath" in text
    assert "run_flaw_hardening_heartbeat_task.cmd" in text
    assert "flaw_hardening_snapshot.json" in text
    assert "flaw_hardening_snapshot.previous.json" in text
    assert "-RepetitionInterval (New-TimeSpan -Minutes 10)" in text
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 3)" in text
    assert '-UserId "NT AUTHORITY\\SYSTEM"' in text
    assert "[switch]$CurrentUser" in text
    assert "-LogonType Interactive -RunLevel Limited" in text
    assert "never submits, cancels, flattens, or promotes" in text


def test_flaw_hardening_heartbeat_is_wired_into_bootstrap_and_runbook() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    normalized_runbook = " ".join(runbook.split())

    assert "register_flaw_hardening_heartbeat_task.ps1" in bootstrap
    assert "ETA-FlawHardeningHeartbeat" in bootstrap
    assert "every 10m read-only flaw snapshot" in bootstrap
    assert "register_flaw_hardening_heartbeat_task.ps1 -Start" in runbook
    assert "flaw_hardening_snapshot.json" in runbook
    assert (
        "scorecard truth, prop-live readiness truth, launch blocker truth, and architecture hotspot counts"
        in normalized_runbook
    )


def test_flaw_hardening_task_scripts_do_not_use_legacy_write_paths() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in (RUNNER, REGISTRAR))

    assert "OneDrive" not in combined
    assert "LOCALAPPDATA" not in combined
    assert "C:\\mnq_data" not in combined
    assert "C:\\crypto_data" not in combined
    assert "TheFirm" not in combined
    assert "The_Firm" not in combined
