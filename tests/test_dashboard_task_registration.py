from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "register_dashboard_api_task.ps1"
RUNNER = ROOT / "deploy" / "scripts" / "run_dashboard_api_task.cmd"
PROXY_SCRIPT = ROOT / "deploy" / "scripts" / "register_proxy8421_bridge_task.ps1"
PROXY_RUNNER = ROOT / "deploy" / "scripts" / "run_proxy8421_task.cmd"
PROXY_WATCHDOG_SCRIPT = ROOT / "deploy" / "scripts" / "register_dashboard_proxy_watchdog_task.ps1"
ETA_WATCHDOG_SCRIPT = ROOT / "deploy" / "scripts" / "register_eta_watchdog_task.ps1"
REPAIR_DASHBOARD_DURABILITY = ROOT / "deploy" / "scripts" / "repair_dashboard_durability_admin.cmd"
DASHBOARD_SYNC_SCRIPT = ROOT / "deploy" / "scripts" / "sync_dashboard_api_live.ps1"
COMMAND_CENTER_SERVICE_XML = ROOT / "deploy" / "FirmCommandCenter_canonical.xml"
ROOT_DIRTY_INSPECT_SCRIPT = ROOT / "deploy" / "scripts" / "inspect_vps_root_dirty.ps1"
ROOT_RECONCILE_PLAN_SCRIPT = ROOT / "deploy" / "scripts" / "plan_vps_root_reconciliation.ps1"
DIAG_COMPACT_SCRIPT = ROOT / "deploy" / "scripts" / "diag_compact.ps1"
FULL_DIAGNOSTICS_SCRIPT = ROOT / "deploy" / "scripts" / "full_diagnostics.ps1"
VPS_BOOTSTRAP_SCRIPTS = (ROOT / "deploy" / "vps_bootstrap.ps1",)
VPS_BOOTSTRAP_SERVICE_SCRIPTS = (
    ROOT / "deploy" / "vps_bootstrap.ps1",
    ROOT / "deploy" / "vps_bootstrap_ascii.ps1",
    ROOT / "deploy" / "vps_bootstrap_clean.ps1",
    ROOT / "deploy" / "vps_bootstrap_v2.ps1",
)


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
    assert "New-ScheduledTaskAction -Execute $Runner" in text
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


def test_command_center_service_template_points_at_canonical_dashboard_api() -> None:
    text = COMMAND_CENTER_SERVICE_XML.read_text(encoding="utf-8")

    assert "eta_engine.deploy.scripts.dashboard_api:app" in text
    assert "command_center.server.app:app" not in text
    assert r"C:\EvolutionaryTradingAlgo" in text
    assert r"firm_command_center" in text
    assert r"\.venv\Scripts\python.exe" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\state" in text
    assert r"C:\EvolutionaryTradingAlgo\logs\eta_engine" in text
    assert "<workingdirectory>C:\\EvolutionaryTradingAlgo</workingdirectory>" in text
    assert "127.0.0.1 --port 8420" in text


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


def test_vps_diagnostics_probe_active_dashboard_ports() -> None:
    compact = DIAG_COMPACT_SCRIPT.read_text(encoding="utf-8")
    full = FULL_DIAGNOSTICS_SCRIPT.read_text(encoding="utf-8")

    assert '4002="IBKR TWS API"' in compact
    assert '8000="Dashboard API"' in compact
    assert '8421="Dashboard proxy"' in compact
    assert '8422="FM status"' in compact
    assert "portfolio/accounts" not in compact
    assert "127.0.0.1:5000" not in compact
    assert "8420" not in compact
    assert '4002 = "IBKR TWS API"' in full
    assert '8000 = "Dashboard API"' in full
    assert '8421 = "Dashboard proxy"' in full
    assert '8422 = "Force Multiplier status"' in full
    assert "portfolio/accounts" not in full
    assert "127.0.0.1:5000" not in full
    assert '8420="Command Center"' not in full
    assert '8420 = "Command Center"' not in full


def test_vps_bootstrap_summaries_name_active_dashboard_topology() -> None:
    for path in VPS_BOOTSTRAP_SCRIPTS:
        text = path.read_text(encoding="utf-8")
        assert "ETA-Dashboard-API" in text
        assert "127.0.0.1:8000 canonical API" in text
        assert "ETA-Proxy-8421" in text
        assert "127.0.0.1:8421 -> 8000" in text
        assert "dashboard on port 8420" not in text
        assert "dashboard (127.0.0.1:8420)" not in text


def test_vps_bootstrap_installs_canonical_command_center_service_template() -> None:
    for path in VPS_BOOTSTRAP_SERVICE_SCRIPTS:
        text = path.read_text(encoding="utf-8")
        assert "FirmCommandCenter_canonical.xml" in text
        assert 'Name="FirmCommandCenter"' in text
        assert 'Xml="FirmCommandCenter.xml"' in text
        assert "$svc.XmlPath" in text


def test_dashboard_proxy_watchdog_task_registration_is_canonical() -> None:
    text = PROXY_WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    assert 'TaskName = "ETA-Dashboard-Proxy-Watchdog"' in text
    assert r"C:\EvolutionaryTradingAlgo" in text
    assert "dashboard_proxy_watchdog.py" in text
    assert "eta_engine.scripts.dashboard_proxy_watchdog" in text
    assert "ETA-Proxy-8421" in text
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "RecoveryIntervalMinutes" in text
    assert "New-ScheduledTaskTrigger -Once" in text
    assert "RepetitionInterval (New-TimeSpan -Minutes $RecoveryIntervalMinutes)" in text
    assert "RepetitionDuration (New-TimeSpan -Days 3650)" in text
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


def test_eta_watchdog_bootstrap_passes_resolved_python() -> None:
    text = (ROOT / "deploy" / "vps_bootstrap.ps1").read_text(encoding="utf-8")

    assert "register_eta_watchdog_task.ps1" in text
    assert "-Root $InstallRoot" in text
    assert "-PythonExe $pythonExe" in text
    assert "-RestartExistingProcess" in text


def test_eta_watchdog_registrar_discovers_python_without_stale_hardcode() -> None:
    text = ETA_WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    assert '[string]$PythonExe = ""' in text
    assert 'Join-Path $EngineDir ".venv\\Scripts\\python.exe"' in text
    assert "Get-Command python" in text
    assert r"C:\Program Files\Python312\python.exe" not in text


def test_dashboard_durability_admin_launcher_repairs_dashboard_and_queue_tasks() -> None:
    text = REPAIR_DASHBOARD_DURABILITY.read_text(encoding="utf-8")

    assert r"ETA_ROOT=C:\EvolutionaryTradingAlgo" in text
    assert "SCRIPT_NAME=repair_dashboard_durability_admin.cmd" in text
    assert "net session" in text
    assert "/DryRun" in text
    assert "--dry-run" in text
    assert "/NoElevate" in text
    assert "--no-elevate" in text
    assert "DRY RUN OK: dashboard durability repair prerequisites are present." in text
    assert "Administrator rights are required to register ETA dashboard durability tasks." in text
    assert "Safe preflight: %SCRIPT_NAME% /DryRun /NoElevate" in text
    assert "Elevated repair: %SCRIPT_NAME%" in text
    assert "Start-Process" in text
    assert "-Verb RunAs" in text
    assert "register_dashboard_api_task.ps1" in text
    assert "register_proxy8421_bridge_task.ps1" in text
    assert "register_dashboard_proxy_watchdog_task.ps1" in text
    assert "register_vps_ops_hardening_audit_task.ps1" in text
    assert "register_operator_queue_heartbeat_task.ps1" in text
    assert "register_paper_live_transition_check_task.ps1" in text
    assert 'File "%REGISTER_DASHBOARD%" -DryRun' in text
    assert 'File "%REGISTER_PROXY%" -WhatIf' in text
    assert 'File "%REGISTER_WATCHDOG%" -WhatIf' in text
    assert 'File "%REGISTER_AUDIT%" -DryRun' in text
    assert 'File "%REGISTER_OPERATOR_QUEUE%" -DryRun' in text
    assert 'File "%REGISTER_PAPER_LIVE%" -DryRun' in text
    assert "paper-live cache tasks only" in text
    assert "vps_ops_hardening_audit --json-out" in text
    assert "never places, cancels, flattens, or promotes orders" in text
    assert "set_ibc_credentials" not in text
    assert "ibgateway_reauth_controller --execute" not in text


def test_dashboard_durability_admin_launcher_avoids_legacy_paths() -> None:
    text = REPAIR_DASHBOARD_DURABILITY.read_text(encoding="utf-8")

    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text


def test_dashboard_sync_script_is_child_only_and_canonical() -> None:
    text = DASHBOARD_SYNC_SCRIPT.read_text(encoding="utf-8")

    assert 'Root = "C:\\EvolutionaryTradingAlgo"' in text
    assert 'Branch = "main"' in text
    assert "paper-live-runtime-hardening" not in text
    assert 'TaskName = "ETA-Dashboard-API"' in text
    assert 'ProbeUri = "http://127.0.0.1:8000/api/bot-fleet"' in text
    assert 'ProxyTaskName = "ETA-Proxy-8421"' in text
    assert 'ProxyProbeUri = "http://127.0.0.1:8421/api/bot-fleet"' in text
    assert "ProbeAttempts = 4" in text
    assert "ProbeTimeoutSeconds = 35" in text
    assert "ProbeRetryDelaySeconds = 5" in text
    assert "SkipProxyRestart" in text
    assert "leaving superproject untouched and syncing eta_engine only" in text
    assert "tracked local changes" in text
    assert "non-overlapping untracked file(s)" in text
    assert "overlap incoming changes" in text
    assert "git ls-files --others --exclude-standard" in text
    assert 'git diff --name-only HEAD.."origin/$Branch"' in text
    assert 'Invoke-Git -WorkingDirectory $EngineDir -Arguments @("pull", "--ff-only", "origin", $Branch)' in text
    assert "Get-ScheduledTask -TaskName $TaskName" in text
    assert "Start-ScheduledTask -TaskName $TaskName" in text
    assert "Get-ScheduledTask -TaskName $ProxyTaskName" in text
    assert "Start-ScheduledTask -TaskName $ProxyTaskName" in text
    assert "Dashboard proxy probe failed after" in text
    assert "proxy_probe_attempt" in text
    assert "Dashboard probe failed after" in text
    assert "probe_attempts" in text
    assert "probe_timeout_seconds" in text
    assert "target_exit_summary" in text
    assert "root_dirty_summary" in text
    assert "deleted_tracked_count" in text
    assert "untracked_count" in text


def test_dashboard_sync_script_avoids_legacy_paths_and_root_pull() -> None:
    text = DASHBOARD_SYNC_SCRIPT.read_text(encoding="utf-8")

    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text
    assert 'Invoke-Git -WorkingDirectory $RootFull -Arguments @("pull"' not in text


def test_root_dirty_inspector_is_read_only_and_canonical() -> None:
    text = ROOT_DIRTY_INSPECT_SCRIPT.read_text(encoding="utf-8")

    assert 'Root = "C:\\EvolutionaryTradingAlgo"' in text
    assert "read_only_inventory" in text
    assert "deleted_tracked" in text
    assert "modified_tracked" in text
    assert "untracked" in text
    assert "submodule_drift" in text
    assert "dirty_companion_repos" in text
    assert "dirty_worktree_sample" in text
    assert "source_or_governance" in text
    assert "generated_market_or_research_artifact" in text
    assert "Manual reconciliation required before cleanup" in text
    assert "destructive_actions_performed = $false" in text
    assert "cleanup_allowed = $false" in text


def test_root_dirty_inspector_avoids_destructive_commands() -> None:
    text = ROOT_DIRTY_INSPECT_SCRIPT.read_text(encoding="utf-8")

    forbidden = (
        "git reset",
        "git clean",
        "git checkout",
        "Remove-Item",
        "Move-Item",
        "git add",
        "git commit",
    )
    for token in forbidden:
        assert token not in text


def test_root_reconciliation_planner_is_review_only() -> None:
    text = ROOT_RECONCILE_PLAN_SCRIPT.read_text(encoding="utf-8")

    assert 'Root = "C:\\EvolutionaryTradingAlgo"' in text
    assert "review_plan_only" in text
    assert "vps_root_dirty_inventory.json" in text
    assert "vps_root_reconciliation_plan.json" in text
    assert "vps_root_reconciliation_plan.md" in text
    assert "manual_review_required" in text
    assert "restore-source-governance" in text
    assert "align-submodules" in text
    assert "classify-generated-artifacts" in text
    assert "dirty_companion_repos" in text
    assert "Dirty companion worktrees" in text
    assert "cleanup_allowed = $false" in text
    assert "destructive_actions_performed = $false" in text


def test_root_reconciliation_planner_avoids_destructive_commands() -> None:
    text = ROOT_RECONCILE_PLAN_SCRIPT.read_text(encoding="utf-8")

    forbidden = (
        "git reset",
        "git clean",
        "git checkout",
        "Remove-Item",
        "Move-Item",
        "git add",
        "git commit",
    )
    for token in forbidden:
        assert token not in text
