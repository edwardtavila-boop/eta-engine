from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "register_jarvis_strategy_supervisor_task.ps1"
RUNNER = ROOT / "deploy" / "scripts" / "run_jarvis_strategy_supervisor_task.cmd"


def test_supervisor_task_registration_is_canonical_and_logged() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'TaskName = "ETA-Jarvis-Strategy-Supervisor"' in text
    assert r"C:\EvolutionaryTradingAlgo\eta_engine" in text
    assert '"logs\\eta_engine"' in text
    assert "jarvis_strategy_supervisor.stdout.log" in text
    assert "jarvis_strategy_supervisor.stderr.log" in text
    assert "run_jarvis_strategy_supervisor_task.cmd" in text
    assert "NT AUTHORITY\\SYSTEM" in text
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "RestartCount 999" in text
    assert 'New-ScheduledTaskAction -Execute $Runner' in text


def test_supervisor_task_registration_avoids_legacy_and_opaque_launchers() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text
    assert "powershell.exe" not in text
    assert "-Command &" not in text
    assert 'cmd.exe" -Argument' not in text


def test_supervisor_task_runner_sets_env_and_redirects_logs() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert r"C:\EvolutionaryTradingAlgo" in text
    assert "ETA_SUPERVISOR_MODE=paper_live" in text
    assert "ETA_SUPERVISOR_FEED=composite" in text
    # 2026-05-05: broker_router is the execution path, but the VPS allowlist
    # keeps unconfigured crypto-paper venues paused until their keys are seeded.
    assert "ETA_PAPER_LIVE_ORDER_ROUTE=broker_router" in text
    assert "ETA_PAPER_LIVE_ALLOWED_SYMBOLS=MNQ,MNQ1,NQ,NQ1" in text
    assert "ETA_RECONCILE_DIVERGENCE_ACK=1" not in text
    assert "ETA_SUPERVISOR_STARTING_CASH=50000" in text
    assert "scripts\\jarvis_strategy_supervisor.py" in text
    assert "jarvis_strategy_supervisor.stdout.log" in text
    assert "jarvis_strategy_supervisor.stderr.log" in text
    assert "python.exe" in text
    assert "exit /b %ERRORLEVEL%" in text


def test_supervisor_task_runner_avoids_legacy_paths() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text
