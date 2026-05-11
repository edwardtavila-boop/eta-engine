from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy" / "scripts" / "run_vps_ops_hardening_audit.cmd"
REGISTRAR = ROOT / "deploy" / "scripts" / "register_vps_ops_hardening_audit_task.ps1"
BOOTSTRAP = ROOT / "deploy" / "vps_bootstrap.ps1"
RUNBOOK = ROOT / "docs" / "VPS_OPS_HARDENING_RUNBOOK.md"


def test_vps_ops_hardening_runner_refreshes_gates_and_canonical_audit() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert r"ETA_ROOT=C:\EvolutionaryTradingAlgo" in text
    assert r"ETA_STATE_DIR=%ETA_ROOT%\var\eta_engine\state" in text
    assert r"ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine" in text
    assert "-m eta_engine.scripts.broker_bracket_audit --json" in text
    assert "-m eta_engine.scripts.prop_strategy_promotion_audit --json" in text
    assert "-m eta_engine.scripts.vps_ops_hardening_audit --json-out --json" in text
    assert "vps_ops_hardening_audit.task.log" in text
    assert "exit /b %AUDIT_RC%" in text


def test_vps_ops_hardening_registrar_is_canonical_read_only_and_low_overhead() -> None:
    text = REGISTRAR.read_text(encoding="utf-8")

    assert "ETA-VpsOpsHardeningAudit" in text
    assert '$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert "Assert-CanonicalEtaPath" in text
    assert "run_vps_ops_hardening_audit.cmd" in text
    assert "vps_ops_hardening_latest.json" in text
    assert "-RepetitionInterval (New-TimeSpan -Minutes 5)" in text
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 2)" in text
    assert '-UserId "NT AUTHORITY\\SYSTEM"' in text
    assert "[switch]$CurrentUser" in text
    assert "-LogonType Interactive -RunLevel Limited" in text
    assert "never submits, cancels, flattens, or promotes" in text


def test_vps_ops_hardening_is_wired_into_bootstrap_and_runbook() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "register_vps_ops_hardening_audit_task.ps1" in bootstrap
    assert "ETA-VpsOpsHardeningAudit" in bootstrap
    assert "every 5m read-only audit" in bootstrap
    assert "register_vps_ops_hardening_audit_task.ps1 -Start" in runbook
    assert "It never submits, cancels, flattens, or promotes orders." in runbook
    assert "promotion_allowed: false" in runbook


def test_vps_ops_hardening_task_scripts_do_not_use_legacy_write_paths() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (RUNNER, REGISTRAR)
    )

    assert "OneDrive" not in combined
    assert "LOCALAPPDATA" not in combined
    assert "C:\\mnq_data" not in combined
    assert "C:\\crypto_data" not in combined
    assert "TheFirm" not in combined
    assert "The_Firm" not in combined
