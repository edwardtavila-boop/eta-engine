from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "register_dashboard_api_task.ps1"
RUNNER = ROOT / "deploy" / "scripts" / "run_dashboard_api_task.cmd"
PROXY_SCRIPT = ROOT / "deploy" / "scripts" / "register_proxy8421_bridge_task.ps1"
PROXY_RUNNER = ROOT / "deploy" / "scripts" / "run_proxy8421_task.cmd"
PROXY_WATCHDOG_SCRIPT = ROOT / "deploy" / "scripts" / "register_dashboard_proxy_watchdog_task.ps1"
PUBLIC_EDGE_WATCHDOG_SCRIPT = ROOT / "deploy" / "scripts" / "register_public_edge_route_watchdog_task.ps1"
ETA_WATCHDOG_SCRIPT = ROOT / "deploy" / "scripts" / "register_eta_watchdog_task.ps1"
REPAIR_DASHBOARD_DURABILITY = ROOT / "deploy" / "scripts" / "repair_dashboard_durability_admin.cmd"
DASHBOARD_SYNC_SCRIPT = ROOT / "deploy" / "scripts" / "sync_dashboard_api_live.ps1"
COMMAND_CENTER_SERVICE_XML = ROOT / "deploy" / "FirmCommandCenter_canonical.xml"
ROOT_DIRTY_INSPECT_SCRIPT = ROOT / "deploy" / "scripts" / "inspect_vps_root_dirty.ps1"
ROOT_RECONCILE_PLAN_SCRIPT = ROOT / "deploy" / "scripts" / "plan_vps_root_reconciliation.ps1"
DIAG_COMPACT_SCRIPT = ROOT / "deploy" / "scripts" / "diag_compact.ps1"
FULL_DIAGNOSTICS_SCRIPT = ROOT / "deploy" / "scripts" / "full_diagnostics.ps1"
DIAG_FAST_SCRIPT = ROOT / "deploy" / "scripts" / "diag_fast.py"
CAPTURE_DAEMONS_SCRIPT = ROOT / "deploy" / "scripts" / "_vps_register_capture_daemons.ps1"
DIAMOND_CRON_SCRIPT = ROOT / "deploy" / "scripts" / "register_diamond_cron_tasks.ps1"
L2_CRON_SCRIPT = ROOT / "deploy" / "scripts" / "register_l2_cron_tasks.ps1"
SCHEDULED_TASK_AUDIT_SCRIPT = ROOT / "deploy" / "scripts" / "audit_vps_scheduled_tasks.ps1"
WEEKLY_SHARPE_REPAIR = ROOT / "deploy" / "scripts" / "repair_eta_weekly_sharpe_task.ps1"
WEEKLY_SHARPE_ADMIN = ROOT / "deploy" / "scripts" / "repair_eta_weekly_sharpe_admin.cmd"
PUBLIC_EDGE_WATCHDOG_REPAIR = ROOT / "deploy" / "scripts" / "repair_eta_public_edge_route_watchdog_task.ps1"
PUBLIC_EDGE_WATCHDOG_ADMIN = ROOT / "deploy" / "scripts" / "repair_eta_public_edge_route_watchdog_admin.cmd"
SCHEDULER_ATTENTION_ADMIN = ROOT / "deploy" / "scripts" / "repair_eta_scheduler_attention_admin.cmd"
FIRM_COMMAND_CENTER_ENV_REPAIR = ROOT / "deploy" / "scripts" / "repair_firm_command_center_env.ps1"
FIRM_COMMAND_CENTER_ENV_ADMIN = ROOT / "deploy" / "scripts" / "repair_firm_command_center_env_admin.cmd"
FORCE_MULTIPLIER_REPAIR = ROOT / "deploy" / "scripts" / "repair_force_multiplier_control_plane.ps1"
FORCE_MULTIPLIER_REPAIR_ADMIN = ROOT / "deploy" / "scripts" / "repair_force_multiplier_control_plane_admin.cmd"
DISABLE_LEGACY_APEX_TASKS_SCRIPT = ROOT / "deploy" / "scripts" / "disable_legacy_apex_tasks.ps1"
TELEGRAM_INBOUND_RUNNER = ROOT / "deploy" / "telegram_inbound_run.bat"
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
    assert "eta_engine.deploy.scripts.dashboard_api:app" in text
    assert "SYSTEM registration unavailable" in text
    assert "WindowsIdentity]::GetCurrent().Name" in text
    assert "LogonType Interactive" in text
    assert "current_user:$currentUser" in text
    assert "PrincipalLabel" not in text


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
    assert "PYTHONPATH=%ETA_ROOT%;%ETA_ENGINE%;%ETA_ENGINE%\\src" in text
    assert 'cd /d "%ETA_ROOT%"' in text
    assert "eta_engine.deploy.scripts.dashboard_api:app" in text
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
    assert r"C:\EvolutionaryTradingAlgo\eta_engine;C:\EvolutionaryTradingAlgo\eta_engine\src" in text
    stale_mirror = "C:\\EvolutionaryTradingAlgo\\firm_command_center" + "\\eta_engine"
    assert f'PYTHONPATH" value="C:\\EvolutionaryTradingAlgo;{stale_mirror}' not in text
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
    assert "SYSTEM registration unavailable" in text
    assert "WindowsIdentity]::GetCurrent().Name" in text
    assert "LogonType Interactive" in text
    assert "current_user:$currentUser" in text
    assert "PrincipalLabel" in text


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
    assert "gateway_authority.json" in compact
    assert "Win32_OperatingSystem" in compact
    assert "strict_vps_checks" in compact
    assert "local_workstation" in compact
    assert "authoritative_vps" in compact
    assert "VPS-targeted expectations are advisory on this host" in compact
    assert "LOCAL_WORKSTATION (VPS expectations advisory)" in compact
    assert "portfolio/accounts" not in compact
    assert "127.0.0.1:5000" not in compact
    assert "8420" not in compact
    assert '4002 = "IBKR TWS API"' in full
    assert '8000 = "Dashboard API"' in full
    assert '8421 = "Dashboard proxy"' in full
    assert '8422 = "Force Multiplier status"' in full
    assert "gateway_authority.json" in full
    assert "Win32_OperatingSystem" in full
    assert "strict_vps_checks" in full
    assert "local_workstation" in full
    assert "authoritative_vps" in full
    assert "VPS-targeted expectations are advisory on this host" in full
    assert "LOCAL_WORKSTATION (VPS expectations advisory)" in full
    assert "portfolio/accounts" not in full
    assert "127.0.0.1:5000" not in full
    assert '8420="Command Center"' not in full
    assert '8420 = "Command Center"' not in full
    assert "audit_vps_scheduled_tasks.ps1" in compact
    assert "ETA-HealthCheck contract drift" in compact
    assert "ETA scheduler attention repair" in compact
    assert "scheduler_attention_task_names" in compact
    assert "scheduler_attention_repair_command" in compact
    assert "FirmCommandCenter runtime Python" in compact
    assert "FirmCommandCenter entrypoint:" in compact
    assert "FirmCommandCenter.err.log" in compact
    assert "command_center_watchdog_status_latest.json" in compact
    assert "operator_repair_prompt_pending" in compact
    assert "operator_repair_pending_command" in compact
    assert "display_issue_summary" in compact
    assert "display_summary" in compact
    assert "-replace '; findings=.*$', ''" in compact
    assert "Eta-CommandCenter-Doctor task contract:" in compact
    assert "ETA dashboard task contract:" in compact
    assert "Command Center watchdog issue:" in compact
    assert "Command Center operator next step:" in compact
    assert "Command Center operator command:" in compact
    assert "Local 8421 contract symptom:" in compact
    assert "Local 8421 upstream probe HTTP codes:" in compact
    assert "ModuleNotFoundError|ImportError" in compact
    assert "No module named '([^']+)'" in compact
    assert "FirmCommandCenter env repair pending UAC approval" in compact
    assert "repair_firm_command_center_env_admin.cmd" in compact
    assert "eta_readiness_snapshot_latest.json" in compact
    assert "public_fallback_stale_flat_open_order_count" in compact
    assert "public_fallback_stale_flat_open_order_symbols" in compact
    assert "public_fallback_stale_flat_open_order_display" in compact
    assert "public_live_broker_open_order_count" in compact
    assert "public_live_broker_degraded_display" in compact
    assert "https://ops.evolutionarytradingalgo.com/api/live/broker_state" in compact
    assert "dashboard_api_runtime_drift_display" in compact
    assert "dashboard_api_runtime_retune_drift_display" in compact
    assert "dashboard_api_runtime_probe_display" in compact
    assert "dashboard_api_runtime_refresh_command" in compact
    assert "dashboard_api_runtime_refresh_requires_elevation" in compact
    assert "public_fallback_broker_open_order_count" in compact
    assert "public_fallback_broker_open_order_drift_display" in compact
    assert "public_fallback_stale_flat_open_order_relation_display" in compact
    assert "retune_focus_active_experiment_drift_display" in compact
    assert "public_live_retune_generated_at_utc" in compact
    assert "public_live_retune_sync_drift_display" in compact
    assert "brackets_summary" in compact
    assert "brackets_next_action" in compact
    assert "checked_at_utc" in compact
    assert "local_retune_generated_at_utc" in compact
    assert "current_local_retune_generated_at_utc" in compact
    assert "ETA_DIAMOND_RETUNE_STATUS_PATH" in compact or "diamond_retune_status_latest.json" in compact
    assert "ETA readiness public fallback:" in compact
    assert "ETA readiness status:" in compact
    assert "ETA readiness primary blocker:" in compact
    assert "ETA readiness detail:" in compact
    assert "ETA readiness primary action:" in compact
    assert "ETA readiness brackets:" in compact
    assert "ETA readiness brackets next:" in compact
    assert "ETA readiness receipt freshness:" in compact
    assert "ETA readiness refresh command:" in compact
    assert "ETA readiness public retune generated:" in compact
    assert "ETA readiness public retune sync drift:" in compact
    assert "ETA readiness current public retune generated:" in compact
    assert "ETA readiness current public retune outcome:" in compact
    assert "ETA readiness current public retune sync drift:" in compact
    assert "ETA readiness cached local retune generated:" in compact
    assert "ETA readiness current local retune generated:" in compact
    assert "ETA readiness broker open orders:" in compact
    assert "ETA readiness live broker_state open orders:" in compact
    assert "ETA readiness stale broker orders:" in compact
    assert "ETA readiness stale-order pressure:" in compact
    assert "ETA readiness public broker_state degraded:" in compact
    assert "ETA readiness current live broker_state degraded:" in compact
    assert "ETA readiness broker-order drift:" in compact
    assert "ETA readiness dashboard API runtime drift:" in compact
    assert "ETA readiness dashboard API runtime retune drift:" in compact
    assert "ETA readiness dashboard API runtime probe:" in compact
    assert "ETA readiness dashboard API runtime refresh:" in compact
    assert "ETA readiness dashboard API runtime refresh requires elevation: true" in compact
    assert "ETA readiness retune mirror drift:" in compact
    assert "ETA readiness local retune sync drift:" in compact
    assert "ETA readiness fallback action:" in compact
    assert "Get-BotFleetProbeCompact" in compact
    assert "http://127.0.0.1:8421/api/bot-fleet" in compact
    assert "http://127.0.0.1:8421/api/master/status" in compact
    assert "via proxy 8421 after direct 8000 miss" in compact
    assert "Dashboard direct API probe missed; proxy recovered:" in compact
    assert "Dashboard API unreachable (direct 8000 and proxy 8421)" in compact
    assert "firm_command_center\\var\\logs" in compact
    assert "audit_vps_scheduled_tasks.ps1" in full
    assert "ETA-HealthCheck contract drift" in full
    assert "ETA scheduler attention repair" in full
    assert "scheduler_attention_task_names" in full
    assert "scheduler_attention_repair_command" in full
    assert "FirmCommandCenter runtime Python" in full
    assert "FirmCommandCenter entrypoint:" in full
    assert "FirmCommandCenter.err.log" in full
    assert "command_center_watchdog_status_latest.json" in full
    assert "operator_repair_prompt_pending" in full
    assert "operator_repair_pending_command" in full
    assert "display_issue_summary" in full
    assert "display_summary" in full
    assert "-replace '; findings=.*$', ''" in full
    assert "Eta-CommandCenter-Doctor task contract:" in full
    assert "ETA dashboard task contract:" in full
    assert "Command Center watchdog issue:" in full
    assert "Command Center operator next step:" in full
    assert "Command Center operator command:" in full
    assert "Local 8421 contract symptom:" in full
    assert "Local 8421 upstream probe HTTP codes:" in full
    assert "Get-Command python.exe" in full
    assert "sys.path.insert(0, r'C:\\EvolutionaryTradingAlgo')" in full
    assert "Python runtime not found for module smoke" in full
    assert "ModuleNotFoundError|ImportError" in full
    assert "No module named '([^']+)'" in full
    assert "FirmCommandCenter env repair pending UAC approval" in full
    assert "repair_firm_command_center_env_admin.cmd" in full
    assert "eta_readiness_snapshot_latest.json" in full
    assert "public_fallback_stale_flat_open_order_count" in full
    assert "public_fallback_stale_flat_open_order_symbols" in full
    assert "public_fallback_stale_flat_open_order_display" in full
    assert "public_live_broker_open_order_count" in full
    assert "public_live_broker_degraded_display" in full
    assert "https://ops.evolutionarytradingalgo.com/api/live/broker_state" in full
    assert "dashboard_api_runtime_drift_display" in full
    assert "dashboard_api_runtime_retune_drift_display" in full
    assert "dashboard_api_runtime_probe_display" in full
    assert "dashboard_api_runtime_refresh_command" in full
    assert "dashboard_api_runtime_refresh_requires_elevation" in full
    assert "public_fallback_broker_open_order_count" in full
    assert "public_fallback_broker_open_order_drift_display" in full
    assert "public_fallback_stale_flat_open_order_relation_display" in full
    assert "retune_focus_active_experiment_drift_display" in full
    assert "public_live_retune_generated_at_utc" in full
    assert "public_live_retune_sync_drift_display" in full
    assert "brackets_summary" in full
    assert "brackets_next_action" in full
    assert "checked_at_utc" in full
    assert "local_retune_generated_at_utc" in full
    assert "current_local_retune_generated_at_utc" in full
    assert "diamond_retune_status_latest.json" in full
    assert "ETA readiness public fallback:" in full
    assert "ETA readiness status:" in full
    assert "ETA readiness primary blocker:" in full
    assert "ETA readiness detail:" in full
    assert "ETA readiness primary action:" in full
    assert "ETA readiness brackets:" in full
    assert "ETA readiness brackets next:" in full
    assert "ETA readiness receipt freshness:" in full
    assert "ETA readiness refresh command:" in full
    assert "ETA readiness public retune generated:" in full
    assert "ETA readiness public retune sync drift:" in full
    assert "ETA readiness current public retune generated:" in full
    assert "ETA readiness current public retune outcome:" in full
    assert "ETA readiness current public retune sync drift:" in full
    assert "ETA readiness cached local retune generated:" in full
    assert "ETA readiness current local retune generated:" in full
    assert "ETA readiness broker open orders:" in full
    assert "ETA readiness live broker_state open orders:" in full
    assert "ETA readiness stale broker orders:" in full
    assert "ETA readiness stale-order pressure:" in full
    assert "ETA readiness public broker_state degraded:" in full
    assert "ETA readiness current live broker_state degraded:" in full
    assert "ETA readiness broker-order drift:" in full
    assert "ETA readiness dashboard API runtime drift:" in full
    assert "ETA readiness dashboard API runtime retune drift:" in full
    assert "ETA readiness dashboard API runtime probe:" in full
    assert "ETA readiness dashboard API runtime refresh:" in full
    assert "ETA readiness dashboard API runtime refresh requires elevation: true" in full
    assert "ETA readiness retune mirror drift:" in full
    assert "ETA readiness local retune sync drift:" in full
    assert "ETA readiness fallback action:" in full
    assert "Get-BotFleetProbe" in full
    assert "http://127.0.0.1:8421/api/bot-fleet" in full
    assert "http://127.0.0.1:8421/api/master/status" in full
    assert "via proxy 8421 after direct 8000 miss" in full
    assert "Dashboard direct API probe missed; proxy recovered:" in full
    assert "Dashboard API unreachable (direct 8000 and proxy 8421)" in full


def test_diag_fast_reads_canonical_eta_readiness_snapshot() -> None:
    text = DIAG_FAST_SCRIPT.read_text(encoding="utf-8")

    assert 'import json' in text
    assert 'ROOT_VAR_DIR / "ops" / "eta_readiness_snapshot_latest.json"' in text
    assert "ETA readiness snapshot exists:" in text
    assert "ETA readiness summary:" in text
    assert 'readiness.get("status")' in text
    assert 'readiness.get("primary_blocker")' in text
    assert 'readiness.get("detail")' in text
    assert 'readiness.get("primary_action")' in text
    assert "Effective status:" in text
    assert "Primary blocker:" in text
    assert "Detail:" in text
    assert "Primary action:" in text
    assert "checked_at_utc" in text
    assert "Receipt freshness:" in text
    assert r"Refresh command: .\scripts\eta-readiness-snapshot.ps1" in text
    assert "public_fallback_reason" in text
    assert "public_live_retune_generated_at_utc" in text
    assert "public_live_retune_sync_drift_display" in text
    assert "brackets_summary" in text
    assert "brackets_next_action" in text
    assert "public_fallback_stale_flat_open_order_display" in text
    assert "public_live_broker_degraded_display" in text
    assert "https://ops.evolutionarytradingalgo.com/api/live/broker_state" in text
    assert "dashboard_api_runtime_drift_display" in text
    assert "dashboard_api_runtime_retune_drift_display" in text
    assert "dashboard_api_runtime_probe_display" in text
    assert "dashboard_api_runtime_refresh_command" in text
    assert "public_fallback_broker_open_order_drift_display" in text
    assert "public_fallback_stale_flat_open_order_relation_display" in text
    assert "retune_focus_active_experiment_drift_display" in text
    assert "local_retune_generated_at_utc" in text
    assert "current_local_retune_generated_at_utc" in text
    assert "ETA_DIAMOND_RETUNE_STATUS_PATH" in text
    assert "Brackets:" in text
    assert "Brackets next:" in text
    assert "Public retune generated:" in text
    assert "Public retune sync drift:" in text
    assert "Current public retune generated:" in text
    assert "Current public retune outcome:" in text
    assert "Current public retune sync drift:" in text
    assert "Cached local retune generated:" in text
    assert "Current local retune generated:" in text
    assert "Public broker_state degraded:" in text
    assert "Current live broker_state degraded:" in text
    assert "Stale-order pressure:" in text
    assert "Broker-order drift:" in text
    assert "Dashboard API runtime drift:" in text
    assert "Dashboard API runtime retune drift:" in text
    assert "Dashboard API runtime probe:" in text
    assert "Dashboard API runtime refresh:" in text
    assert "Dashboard API runtime refresh requires elevation: true" in text
    assert "Retune mirror drift:" in text
    assert "Local retune sync drift:" in text
    assert 'http://127.0.0.1:8421/api/master/status' in text


def test_vps_bootstrap_summaries_name_active_dashboard_topology() -> None:
    for path in VPS_BOOTSTRAP_SCRIPTS:
        text = path.read_text(encoding="utf-8")
        assert "ETA-Dashboard-API" in text
        assert "127.0.0.1:8000 canonical API" in text
        assert "ETA-Proxy-8421" in text
        assert "127.0.0.1:8421 -> 8000" in text
        assert "ETA-FM-HealthProbe" in text
        assert "every 15m cached Force Multiplier health" in text
        assert "install_fm_health_task.ps1" in text
        assert "dashboard on port 8420" not in text
        assert "dashboard (127.0.0.1:8420)" not in text


def test_vps_bootstrap_installs_canonical_command_center_service_template() -> None:
    for path in VPS_BOOTSTRAP_SERVICE_SCRIPTS:
        text = path.read_text(encoding="utf-8")
        assert "FirmCommandCenter_canonical.xml" in text
        assert 'Name="FirmCommandCenter"' in text
        assert 'Xml="FirmCommandCenter.xml"' in text
        assert "$svc.XmlPath" in text
        if path.name == "vps_bootstrap.ps1":
            assert "$serviceExe = \"$svcDir\\$($svc.Name).exe\"" in text
            assert "Copy-Item $winswExe $serviceExe -Force" in text
            assert '& "$svcDir\\winsw.exe" install' not in text


def test_force_multiplier_control_plane_repair_reuses_canonical_service_and_task_registrar() -> None:
    text = FORCE_MULTIPLIER_REPAIR.read_text(encoding="utf-8")

    assert 'WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert "FmStatusServer.xml" in text
    assert "firm_command_center\\services" in text
    assert "winsw.exe" in text
    assert "register_codex_operator_task.ps1" in text
    assert "ETA-ThreeAI-Sync" in text
    assert '$ServiceXmlTarget = Join-Path $ServicesRoot "$ServiceName.xml"' in text
    assert '$ServiceExe = Join-Path $ServicesRoot "$ServiceName.exe"' in text
    assert "flat_winsw_service" in text
    assert "legacy_nested_service_dir" in text
    assert '$ServiceDir = Join-Path $ServicesRoot $ServiceName' not in text
    assert "eta_engine.scripts.force_multiplier_health --json-out --quiet" in text
    assert "eta_engine.scripts.vps_ops_hardening_audit --json-out" in text
    assert "Update-FmStatusServiceXmlExecutable" in text
    assert "Resolve-EtaPython" in text
    assert "Stop-SafeFmStatusPortOwner" in text
    assert "fm_status_server:app" in text
    assert "broker_order_actions = $false" in text
    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text


def test_force_multiplier_control_plane_admin_launcher_is_safe_and_dry_runnable() -> None:
    text = FORCE_MULTIPLIER_REPAIR_ADMIN.read_text(encoding="utf-8")

    assert "repair_force_multiplier_control_plane.ps1" in text
    assert "/DryRun" in text
    assert "/NoElevate" in text
    assert "/RestartService" in text
    assert "Start-Process" in text
    assert 'set "SELF=%~f0"' in text
    assert "Start-Process -FilePath '%SELF%'" in text
    assert "Verb RunAs" in text
    assert "never places, cancels, flattens, or promotes orders" in text
    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text


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
    assert "--once --json" in text
    assert "--interval-s" not in text
    assert "ExecutionTimeLimit (New-TimeSpan -Minutes 2)" in text
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


def test_public_edge_route_watchdog_task_registration_is_canonical() -> None:
    text = PUBLIC_EDGE_WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    assert 'TaskName = "ETA-Public-Edge-Route-Watchdog"' in text
    assert r"C:\EvolutionaryTradingAlgo" in text
    assert "public_edge_route_watchdog.py" in text
    assert "eta_engine.scripts.public_edge_route_watchdog" in text
    assert "Get-CimInstance Win32_Process" in text
    assert "Stop-ScheduledTask -TaskName $TaskName" in text
    assert "Unregister-ScheduledTask -TaskName $TaskName" in text
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "RecoveryIntervalMinutes" in text
    assert "New-ScheduledTaskTrigger -Once" in text
    assert "RepetitionInterval (New-TimeSpan -Minutes $RecoveryIntervalMinutes)" in text
    assert "RestartCount 999" in text
    assert "--once --json" in text
    assert "127.0.0.1:8081" in text
    assert "127.0.0.1:8421" in text
    assert "FirmCommandCenterEdge route drift" in text
    assert "[switch]$CurrentUser" in text
    assert "Administrator rights are required to register $TaskName as SYSTEM" in text
    assert "repair_eta_public_edge_route_watchdog_admin.cmd" in text
    assert "WindowsIdentity]::GetCurrent().Name" in text
    assert "LogonType Interactive" in text
    assert "PrincipalLabel" in text
    assert "Explicit current-user fallback with recurring route checks." in text


def test_public_edge_route_watchdog_task_registration_avoids_legacy_paths() -> None:
    text = PUBLIC_EDGE_WATCHDOG_SCRIPT.read_text(encoding="utf-8")

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


def test_public_edge_route_watchdog_registrar_requires_explicit_current_user_fallback() -> None:
    text = PUBLIC_EDGE_WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    assert "[switch]$CurrentUser" in text
    assert 'if ($CurrentUser) {' in text
    assert "WindowsIdentity]::GetCurrent().Name" in text
    assert "LogonType Interactive" in text
    assert "current_user:$currentUser" in text
    assert "Administrator rights are required to register $TaskName as SYSTEM" in text
    assert "SYSTEM registration unavailable" not in text
    assert "Write-Warning" not in text


def test_eta_watchdog_registrar_falls_back_when_system_task_is_denied() -> None:
    text = ETA_WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    assert "SYSTEM registration unavailable" in text
    assert "WindowsIdentity]::GetCurrent().Name" in text
    assert "LogonType Interactive" in text
    assert "current_user:$currentUser" in text
    assert "PrincipalLabel" in text


def test_capture_daemon_registrar_uses_runtime_parameters_without_hardcoded_user() -> None:
    text = CAPTURE_DAEMONS_SCRIPT.read_text(encoding="utf-8")

    assert '[string]$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert '[string]$PythonPath = ""' in text
    assert '[string]$TaskUser = ""' in text
    assert "[switch]$StartNow" in text
    assert 'GetEnvironmentVariable("ETA_PYTHON_EXE", "Machine")' in text
    assert 'GetEnvironmentVariable("ETA_TASK_USER", "Machine")' in text
    assert "WindowsIdentity]::GetCurrent().Name" in text
    assert '$TaskUser = "$env:COMPUTERNAME\\$env:USERNAME"' in text
    assert 'Join-Path $WorkspaceRoot "eta_engine\\.venv\\Scripts\\python.exe"' in text
    assert "Get-CimInstance Win32_Process" in text
    assert "Stop-Process -Id $worker.ProcessId -Force" in text
    assert "capture_depth_snapshots" in text
    assert "capture_tick_stream" in text
    assert "if ($StartNow)" in text
    for symbol in ('"MNQ"', '"NQ"', '"ES"', '"M2K"', '"MYM"', '"6E"', '"MBT"', '"MCL"', '"NG"'):
        assert symbol in text
    assert "--max-active-tick-requests 5 --rotation-seconds 20" in text
    assert "--max-active-depth-requests 3 --rotation-seconds 20" in text
    assert "fxut9145410\\trader" not in text


def test_cron_task_registrars_prefer_workspace_venv_python() -> None:
    for path in (DIAMOND_CRON_SCRIPT, L2_CRON_SCRIPT):
        text = path.read_text(encoding="utf-8")
        assert '[string]$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
        assert 'Join-Path $WorkspaceRoot "eta_engine\\.venv\\Scripts\\python.exe"' in text
        assert 'GetEnvironmentVariable("ETA_PYTHON_EXE", "Machine")' in text
        assert 'GetEnvironmentVariable("ETA_TASK_USER", "Machine")' in text
        assert "Get-Command python" in text


def test_diamond_cron_registrar_falls_back_when_system_task_is_denied() -> None:
    text = DIAMOND_CRON_SCRIPT.read_text(encoding="utf-8")
    assert "WindowsIdentity]::GetCurrent().Name" in text
    assert "SYSTEM registration unavailable for ${Name}" in text
    assert "LogonType Interactive" in text
    assert 'current_user:$currentUser' in text


def test_scheduled_task_audit_is_read_only_and_surfaces_legacy_paths() -> None:
    text = SCHEDULED_TASK_AUDIT_SCRIPT.read_text(encoding="utf-8")

    assert '[string]$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert "[switch]$Json" in text
    assert "Get-ScheduledTask" in text
    assert "needs_attention_count" in text
    assert "uses_canonical" in text
    assert "canonical_basis" in text
    assert "uses_legacy_path" in text
    assert "operator_refused_request" in text
    assert "operator_refused_request_interactive_principal" in text
    assert "interactive_principal_service_control_risk" in text
    assert "principal_user_id" in text
    assert "principal_logon_type" in text
    assert "is_current_user_interactive_principal" in text
    assert "recommended_repair_command" in text
    assert ".\\eta_engine\\deploy\\scripts\\repair_eta_public_edge_route_watchdog_admin.cmd" in text
    assert ".\\eta_engine\\deploy\\scripts\\repair_eta_weekly_sharpe_admin.cmd" in text
    assert "scheduler_attention_task_names" in text
    assert "scheduler_attention_repair_command" in text
    assert ".\\eta_engine\\deploy\\scripts\\repair_eta_scheduler_attention_admin.cmd" in text
    assert '$expectedCriticalTasks = @(' in text
    assert 'result_class = "missing_task"' in text
    assert "allows_nonzero_verdict" in text
    assert "$usesEtaModule = $actions -match" in text
    assert "eta_module_invocation" in text
    assert '$verdictExitTasks = @{' in text
    assert '"ETA-Diamond-FirstLightCheck"' in text
    assert '"ETA-Diamond-LaunchReadinessEvery15Min"' in text
    assert '"ETA-Diamond-OpsDashboardHourly"' in text
    assert '"ETA-WeeklySharpe"' in text
    assert "verdict_no_go" in text
    assert "verdict_p0_critical" in text
    assert "verdict_amber" in text
    assert "verdict_red" in text
    assert "C:\\eta_engine" in text
    assert "AppData\\Local\\eta_engine" in text
    assert "C:\\apex_predator" in text
    for forbidden in ("Disable-ScheduledTask", "Unregister-ScheduledTask", "Register-ScheduledTask"):
        assert forbidden not in text


def test_weekly_sharpe_repair_script_prefers_system_principal_and_canonical_module() -> None:
    text = WEEKLY_SHARPE_REPAIR.read_text(encoding="utf-8")

    assert "[CmdletBinding()]" in text
    assert "[switch]$DryRun" in text
    assert "[switch]$CurrentUser" in text
    assert "[switch]$Start" in text
    assert '$taskName = "ETA-WeeklySharpe"' in text
    assert '$workspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert '$scriptModule = "eta_engine.scripts.weekly_sharpe_check"' in text
    assert 'Join-Path $etaEngineRoot "scripts\\weekly_sharpe_check.py"' in text
    assert 'Join-Path $etaEngineRoot ".venv\\Scripts\\python.exe"' in text
    assert '$machinePython = "C:\\Python314\\python.exe"' in text
    assert 'New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "11:00 PM"' in text
    assert '-Argument "-m $scriptModule"' in text
    assert "Administrator rights are required to register $taskName as SYSTEM" in text
    assert '-UserId "NT AUTHORITY\\SYSTEM"' in text
    assert "-LogonType ServiceAccount" in text
    assert "-RunLevel Highest" in text
    assert 'if ($CurrentUser) {' in text
    assert "-LogonType Interactive" in text
    assert "Start-ScheduledTask -TaskName $taskName" in text


def test_weekly_sharpe_admin_launcher_self_elevates_and_supports_dry_run() -> None:
    text = WEEKLY_SHARPE_ADMIN.read_text(encoding="utf-8")

    assert r'set "ETA_ROOT=C:\EvolutionaryTradingAlgo"' in text
    assert r'set "REPAIR_PS1=%SCRIPTS%\repair_eta_weekly_sharpe_task.ps1"' in text
    assert "/DryRun" in text
    assert "/NoElevate" in text
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_PS1%" -DryRun' in text
    assert "net session" in text
    assert "Start-Process -FilePath '%~f0'" in text
    assert "-Verb RunAs" in text
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_PS1%" -Start' in text


def test_public_edge_watchdog_repair_script_prefers_system_principal_and_canonical_module() -> None:
    text = PUBLIC_EDGE_WATCHDOG_REPAIR.read_text(encoding="utf-8")

    assert "[CmdletBinding()]" in text
    assert "[switch]$DryRun" in text
    assert "[switch]$CurrentUser" in text
    assert "[switch]$Start" in text
    assert '[int]$RecoveryIntervalMinutes = 5' in text
    assert '$taskName = "ETA-Public-Edge-Route-Watchdog"' in text
    assert '$workspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert '$scriptModule = "eta_engine.scripts.public_edge_route_watchdog"' in text
    assert 'Join-Path $etaEngineRoot "scripts\\public_edge_route_watchdog.py"' in text
    assert 'Join-Path $etaEngineRoot ".venv\\Scripts\\python.exe"' in text
    assert '$machinePython = "C:\\Python314\\python.exe"' in text
    assert '-Argument "-m $scriptModule --once --json"' in text
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "-RepetitionInterval (New-TimeSpan -Minutes $RecoveryIntervalMinutes)" in text
    assert "Administrator rights are required to register $taskName as SYSTEM" in text
    assert '-UserId "NT AUTHORITY\\SYSTEM"' in text
    assert "-LogonType ServiceAccount" in text
    assert "-RunLevel Highest" in text
    assert 'if ($CurrentUser) {' in text
    assert "-LogonType Interactive" in text
    assert "Start-ScheduledTask -TaskName $taskName" in text


def test_public_edge_watchdog_admin_launcher_self_elevates_and_supports_dry_run() -> None:
    text = PUBLIC_EDGE_WATCHDOG_ADMIN.read_text(encoding="utf-8")

    assert r'set "ETA_ROOT=C:\EvolutionaryTradingAlgo"' in text
    assert r'set "REPAIR_PS1=%SCRIPTS%\repair_eta_public_edge_route_watchdog_task.ps1"' in text
    assert "/DryRun" in text
    assert "/NoElevate" in text
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_PS1%" -DryRun' in text
    assert "net session" in text
    assert "Start-Process -FilePath '%~f0'" in text
    assert "-Verb RunAs" in text
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_PS1%" -Start' in text


def test_firm_command_center_env_repair_script_uses_locked_service_project_and_probe() -> None:
    text = FIRM_COMMAND_CENTER_ENV_REPAIR.read_text(encoding="utf-8")

    assert "[CmdletBinding()]" in text
    assert "[switch]$DryRun" in text
    assert "[switch]$Start" in text
    assert '$serviceName = "FirmCommandCenter"' in text
    assert '$workspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert 'Join-Path $serviceRoot "eta_engine"' in text
    assert 'Join-Path $serviceRoot "services\\FirmCommandCenter.xml"' in text
    assert 'Join-Path $projectRoot "pyproject.toml"' in text
    assert 'Join-Path $projectRoot "uv.lock"' in text
    assert 'Join-Path $projectRoot ".venv\\Scripts\\python.exe"' in text
    assert "Get-Command uv" in text
    assert "uv sync --locked" in text
    assert "Get-ServiceImportProbe" in text
    assert "service.arguments" in text
    assert '$importModules.Add("uvicorn")' in text
    assert '"importlib.import_module(' in text
    assert "eta_engine.deploy.scripts.dashboard_api" in text
    assert "service_arguments = $serviceImportProbe.service_arguments" in text
    assert "import_probe_modules = @($serviceImportProbe.import_modules)" in text
    assert "probe_command = $serviceImportProbe.probe_command" in text
    assert 'repair_command = $adminRepairCommand' in text
    assert "Administrator rights are required to repair $serviceName" in text
    assert "Stop-Service -Name $serviceName -Force" in text
    assert "Start-Service -Name $serviceName" in text


def test_firm_command_center_env_admin_launcher_self_elevates_and_supports_dry_run() -> None:
    text = FIRM_COMMAND_CENTER_ENV_ADMIN.read_text(encoding="utf-8")

    assert r'set "ETA_ROOT=C:\EvolutionaryTradingAlgo"' in text
    assert r'set "REPAIR_PS1=%SCRIPTS%\repair_firm_command_center_env.ps1"' in text
    assert "/DryRun" in text
    assert "/NoElevate" in text
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_PS1%" -DryRun' in text
    assert "net session" in text
    assert "Start-Process -FilePath '%~f0'" in text
    assert "-Verb RunAs" in text
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_PS1%" -Start' in text


def test_scheduler_attention_admin_launcher_self_elevates_and_batches_repairs() -> None:
    text = SCHEDULER_ATTENTION_ADMIN.read_text(encoding="utf-8")

    assert r'set "ETA_ROOT=C:\EvolutionaryTradingAlgo"' in text
    assert r'set "REPAIR_PUBLIC_EDGE=%SCRIPTS%\repair_eta_public_edge_route_watchdog_admin.cmd"' in text
    assert r'set "REPAIR_WEEKLY_SHARPE=%SCRIPTS%\repair_eta_weekly_sharpe_admin.cmd"' in text
    assert "/DryRun" in text
    assert "/NoElevate" in text
    assert 'call "%REPAIR_PUBLIC_EDGE%" /DryRun /NoElevate' in text
    assert 'call "%REPAIR_WEEKLY_SHARPE%" /DryRun /NoElevate' in text
    assert "net session" in text
    assert "Start-Process -FilePath '%~f0'" in text
    assert "-Verb RunAs" in text
    assert 'call "%REPAIR_PUBLIC_EDGE%"' in text
    assert 'call "%REPAIR_WEEKLY_SHARPE%"' in text
    assert "ETA-Public-Edge-Route-Watchdog and ETA-WeeklySharpe" in text


def test_scheduled_task_audit_flags_duplicate_paper_live_supervisor() -> None:
    text = SCHEDULED_TASK_AUDIT_SCRIPT.read_text(encoding="utf-8")

    assert "ETA-PaperLive-Supervisor" in text
    assert "ETAJarvisSupervisor" in text
    assert "disable_duplicate_and_keep_${canonicalSupervisorService}_service_fallback_${canonicalSupervisorTaskFallback}" in text
    assert '"disable_duplicate_and_keep_ETA-Jarvis-Strategy-Supervisor"' not in text
    assert "unsafe_duplicate_supervisor" in text
    assert "is_unsafe_duplicate_supervisor" in text


def test_legacy_apex_disabler_is_apply_gated_and_canonical_backup_only() -> None:
    text = DISABLE_LEGACY_APEX_TASKS_SCRIPT.read_text(encoding="utf-8")

    assert '[string]$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert "[switch]$Apply" in text
    assert "Assert-CanonicalEtaPath -Path $WorkspaceRoot" in text
    assert "Assert-CanonicalEtaPath -Path $BackupRoot" in text
    assert '"would_disable"' in text
    assert 'if ($Apply)' in text
    assert "Export-ScheduledTask" in text
    assert "Disable-ScheduledTask" in text
    assert "Unregister-ScheduledTask" not in text


def test_telegram_inbound_runner_uses_eta_venv_not_hermes_admin_venv() -> None:
    text = TELEGRAM_INBOUND_RUNNER.read_text(encoding="utf-8")

    assert r"set ETA_ENGINE=C:\EvolutionaryTradingAlgo\eta_engine" in text
    assert r"set PYTHON_EXE=%ETA_ENGINE%\.venv\Scripts\python.exe" in text
    assert r'"%PYTHON_EXE%" ^' in text
    assert r"C:\Users\Administrator\.hermes" not in text


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
    assert "register_public_edge_route_watchdog_task.ps1" in text
    assert "register_vps_ops_hardening_audit_task.ps1" in text
    assert "register_operator_queue_heartbeat_task.ps1" in text
    assert "register_paper_live_transition_check_task.ps1" in text
    assert "repair_eta_public_edge_route_watchdog_task.ps1" in text
    assert "repair_eta_healthcheck_task.ps1" in text
    assert "repair_firm_command_center_env.ps1" in text
    assert 'File "%REGISTER_DASHBOARD%" -DryRun' in text
    assert 'File "%REGISTER_PROXY%" -WhatIf' in text
    assert 'File "%REGISTER_WATCHDOG%" -WhatIf' in text
    assert 'File "%REGISTER_PUBLIC_EDGE%" -WhatIf' in text
    assert 'File "%REGISTER_AUDIT%" -DryRun' in text
    assert 'File "%REGISTER_OPERATOR_QUEUE%" -DryRun' in text
    assert 'File "%REGISTER_PAPER_LIVE%" -DryRun' in text
    assert 'File "%REPAIR_PUBLIC_EDGE_WATCHDOG%" -DryRun' in text
    assert 'File "%REPAIR_HEALTHCHECK%" -DryRun' in text
    assert 'File "%REPAIR_FIRM_COMMAND_CENTER_ENV%" -DryRun' in text
    assert 'File "%REPAIR_PUBLIC_EDGE_WATCHDOG%"' in text
    assert 'File "%REPAIR_HEALTHCHECK%"' in text
    assert 'File "%REPAIR_FIRM_COMMAND_CENTER_ENV%" -Start' in text
    assert "paper-live cache tasks only" in text
    assert "vps_ops_hardening_audit --json-out" in text
    assert "ETA-Public-Edge-Route-Watchdog, ETA-HealthCheck, and FirmCommandCenter env repair" in text
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


def test_vps_bootstrap_registers_public_edge_route_watchdog() -> None:
    text = (ROOT / "deploy" / "vps_bootstrap.ps1").read_text(encoding="utf-8")

    assert "register_public_edge_route_watchdog_task.ps1" in text
    assert "Registering public edge route watchdog task" in text
    assert "ETA-Public-Edge-Route-Watchdog" in text
    assert "8081 route self-heal" in text


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
