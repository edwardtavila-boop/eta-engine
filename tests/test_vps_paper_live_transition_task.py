from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy" / "scripts" / "run_paper_live_transition_check.cmd"
REGISTRAR = ROOT / "deploy" / "scripts" / "register_paper_live_transition_check_task.ps1"
BOOTSTRAP = ROOT / "deploy" / "vps_bootstrap.ps1"
RUNBOOK = ROOT / "docs" / "live_launch_runbook.md"


def test_paper_live_transition_runner_writes_canonical_cache_without_task_failure() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert r"ETA_ROOT=C:\EvolutionaryTradingAlgo" in text
    assert r"ETA_STATE_DIR=%ETA_ROOT%\var\eta_engine\state" in text
    assert r"ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine" in text
    assert "python.exe" in text
    assert "-m eta_engine.scripts.bot_strategy_readiness" in text
    assert "--scope supervisor_pinned" in text
    assert "--snapshot" in text
    assert "READINESS_STDOUT_TMP=%ETA_LOG_DIR%\\bot_strategy_readiness.%RUN_ID%.stdout.tmp.log" in text
    assert "READINESS_STDERR_TMP=%ETA_LOG_DIR%\\bot_strategy_readiness.%RUN_ID%.stderr.tmp.log" in text
    assert "bot_strategy_readiness.stdout.log" in text
    assert "bot_strategy_readiness.stderr.log" in text
    assert "bot_strategy_readiness.task.log" in text
    assert "-m eta_engine.scripts.paper_live_transition_check" in text
    assert "RUN_ID=%RANDOM%_%RANDOM%" in text
    assert "STDOUT_TMP=%ETA_LOG_DIR%\\paper_live_transition_check.%RUN_ID%.stdout.tmp.log" in text
    assert "STDERR_TMP=%ETA_LOG_DIR%\\paper_live_transition_check.%RUN_ID%.stderr.tmp.log" in text
    assert "paper_live_transition_check.stdout.log" in text
    assert "paper_live_transition_check.stderr.log" in text
    assert "paper_live_transition_check.task.log" in text
    assert "exit_code=%CHECK_RC%" in text
    assert '1> "%STDOUT_TMP%"' in text
    assert '2> "%STDERR_TMP%"' in text
    assert '1>> "%ETA_LOG_DIR%\\paper_live_transition_check.stdout.log"' not in text
    assert "exit /b 0" in text


def test_paper_live_transition_registrar_is_canonical_and_low_overhead() -> None:
    text = REGISTRAR.read_text(encoding="utf-8")

    assert "ETA-PaperLiveTransitionCheck" in text
    assert '$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert "Assert-CanonicalEtaPath" in text
    assert "run_paper_live_transition_check.cmd" in text
    assert "paper_live_transition_check.json" in text
    assert "-RepetitionInterval (New-TimeSpan -Minutes 5)" in text
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 2)" in text
    assert "-MultipleInstances IgnoreNew" in text
    assert '-UserId "NT AUTHORITY\\SYSTEM"' in text
    assert "Start-ScheduledTask -TaskName $TaskName" in text


def test_paper_live_transition_task_is_wired_into_bootstrap_and_runbook() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "register_paper_live_transition_check_task.ps1" in bootstrap
    assert "ETA-PaperLiveTransitionCheck" in bootstrap
    assert "paper-live transition cache refresher task" in bootstrap
    assert "register_paper_live_transition_check_task.ps1 -Start" in runbook
    assert "never clears holds or submits orders" in runbook


def test_paper_live_transition_task_scripts_do_not_use_legacy_write_paths() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in (RUNNER, REGISTRAR))

    assert "OneDrive" not in combined
    assert "LOCALAPPDATA" not in combined
    assert "mnq_data" not in combined
    assert "crypto_data" not in combined
    assert "TheFirm" not in combined
    assert "The_Firm" not in combined
