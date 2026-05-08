from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "register_dashboard_api_task.ps1"
RUNNER = ROOT / "deploy" / "scripts" / "run_dashboard_api_task.cmd"
PROXY_SCRIPT = ROOT / "deploy" / "scripts" / "register_proxy8421_bridge_task.ps1"
PROXY_RUNNER = ROOT / "deploy" / "scripts" / "run_proxy8421_task.cmd"
PROXY_WATCHDOG_SCRIPT = ROOT / "deploy" / "scripts" / "register_dashboard_proxy_watchdog_task.ps1"
DASHBOARD_SYNC_SCRIPT = ROOT / "deploy" / "scripts" / "sync_dashboard_api_live.ps1"


def test_dashboard_api_task_registration_is_canonical_and_logged() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'TaskName = "ETA-Dashboard-API"' in text
    assert r"C:\EvolutionaryTradingAlgo\eta_engine" in text
    assert r"C:\EvolutionaryTradingAlgo" in text
    assert '"logs\\eta_engine"' in text
    assert "dashboard_api.stdout.log" in text
    assert "dashboard_api.stderr.log" in text
    assert "run_dashboard_api_task.cmd" in text
    assert "NT AUTHORITY\\SYSTEM" in text
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "RestartCount 999" in text
    assert 'New-ScheduledTaskAction -Execute $Runner' in text
    assert "Start-ScheduledTask -TaskName $TaskName" in text
    assert "Get-CimInstance Win32_Process" in text
    assert "deploy.scripts.dashboard_api:app" in text


def test_dashboard_api_task_registration_avoids_legacy_and_inline_launchers() -> None:
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
    assert "_start_dash.py" not in text


def test_dashboard_api_task_runner_sets_env_and_redirects_logs() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert r"C:\EvolutionaryTradingAlgo" in text
    assert "ETA_STATE_DIR=%ETA_ROOT%\\var\\eta_engine\\state" in text
    assert "ETA_LOG_DIR=%ETA_ROOT%\\logs\\eta_engine" in text
    assert "ETA_DASHBOARD_HOST=127.0.0.1" in text
    assert "ETA_DASHBOARD_PORT=8000" in text
    assert "deploy.scripts.dashboard_api:app" in text
    assert "dashboard_api.stdout.log" in text
    assert "dashboard_api.stderr.log" in text
    assert "python.exe" in text
    assert "exit /b %ERRORLEVEL%" in text


def test_dashboard_api_task_runner_avoids_legacy_paths() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text


def test_proxy8421_task_registration_replaces_stale_workers() -> None:
    text = PROXY_SCRIPT.read_text(encoding="utf-8")

    assert 'TaskName = "ETA-Proxy-8421"' in text
    assert r"C:\EvolutionaryTradingAlgo\eta_engine" in text
    assert "reverse_proxy_bridge.py" in text
    assert "run_proxy8421_task.cmd" in text
    assert "Get-CimInstance Win32_Process" in text
    assert "Stop-ScheduledTask -TaskName $TaskName" in text
    assert "Unregister-ScheduledTask -TaskName $TaskName" in text
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "RestartCount 999" in text
    assert "ExecutionTimeLimit ([TimeSpan]::Zero)" in text
    assert 'ListenHost = "127.0.0.1"' in text
    assert "ListenPort = 8421" in text
    assert "http://127.0.0.1:8000" in text


def test_proxy8421_task_registration_avoids_legacy_paths() -> None:
    text = PROXY_SCRIPT.read_text(encoding="utf-8")

    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text


def test_proxy8421_task_runner_is_canonical_and_logged() -> None:
    text = PROXY_RUNNER.read_text(encoding="utf-8")

    assert r"C:\EvolutionaryTradingAlgo" in text
    assert "ETA_PROXY_HOST=127.0.0.1" in text
    assert "ETA_PROXY_PORT=8421" in text
    assert "ETA_PROXY_TARGET=http://127.0.0.1:8000" in text
    assert "reverse_proxy_bridge.py" in text
    assert "proxy_8421.stdout.log" in text
    assert "proxy_8421.stderr.log" in text
    assert "python.exe" in text
    assert "exit /b %ERRORLEVEL%" in text


def test_proxy8421_task_runner_avoids_legacy_paths() -> None:
    text = PROXY_RUNNER.read_text(encoding="utf-8")

    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text


def test_dashboard_proxy_watchdog_task_registration_is_canonical() -> None:
    text = PROXY_WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    assert 'TaskName = "ETA-Dashboard-Proxy-Watchdog"' in text
    assert r"C:\EvolutionaryTradingAlgo" in text
    assert "dashboard_proxy_watchdog.py" in text
    assert "eta_engine.scripts.dashboard_proxy_watchdog" in text
    assert "ETA-Proxy-8421" in text
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "RestartCount 999" in text
    assert "ExecutionTimeLimit ([TimeSpan]::Zero)" in text
    assert "Start-ScheduledTask -TaskName $TaskName" in text
    assert "dashboard_proxy_watchdog" in text


def test_dashboard_proxy_watchdog_task_registration_avoids_legacy_paths() -> None:
    text = PROXY_WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text


def test_dashboard_sync_script_is_child_only_and_canonical() -> None:
    text = DASHBOARD_SYNC_SCRIPT.read_text(encoding="utf-8")

    assert 'Root = "C:\\EvolutionaryTradingAlgo"' in text
    assert 'Branch = "codex/paper-live-runtime-hardening"' in text
    assert 'TaskName = "ETA-Dashboard-API"' in text
    assert 'ProbeUri = "http://127.0.0.1:8000/api/bot-fleet"' in text
    assert "leaving superproject untouched and syncing eta_engine only" in text
    assert 'Invoke-Git -WorkingDirectory $EngineDir -Arguments @("pull", "--ff-only", "origin", $Branch)' in text
    assert 'Get-ScheduledTask -TaskName $TaskName' in text
    assert 'Start-ScheduledTask -TaskName $TaskName' in text
    assert "target_exit_summary" in text


def test_dashboard_sync_script_avoids_legacy_paths_and_root_pull() -> None:
    text = DASHBOARD_SYNC_SCRIPT.read_text(encoding="utf-8")

    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text
    assert 'Invoke-Git -WorkingDirectory $RootFull -Arguments @("pull"' not in text
