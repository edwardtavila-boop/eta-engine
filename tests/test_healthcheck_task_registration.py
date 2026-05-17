from __future__ import annotations

from pathlib import Path

ETA_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ETA_ROOT.parent
BOOTSTRAP = ETA_ROOT / "deploy" / "vps_bootstrap.ps1"
VARIANT_BOOTSTRAPS = (
    ETA_ROOT / "deploy" / "vps_bootstrap_ascii.ps1",
    ETA_ROOT / "deploy" / "vps_bootstrap_clean.ps1",
    ETA_ROOT / "deploy" / "vps_bootstrap_v2.ps1",
    ETA_ROOT / "deploy" / "vps_bootstrap_v1_legacy.ps1",
)
REPAIR = ETA_ROOT / "deploy" / "scripts" / "repair_eta_healthcheck_task.ps1"
REPAIR_DURABILITY = ETA_ROOT / "deploy" / "scripts" / "repair_dashboard_durability_admin.cmd"
SCHEDULED_TASK_AUDIT = ETA_ROOT / "deploy" / "scripts" / "audit_vps_scheduled_tasks.ps1"
RUNBOOK = ETA_ROOT / "docs" / "VPS_OPS_HARDENING_RUNBOOK.md"
CANONICAL_VPS_FIX = ETA_ROOT / "deploy" / "canonical_vps_fix.py"
HEALTH_CHECK_SCRIPT = ETA_ROOT / "scripts" / "health_check.py"
LEGACY_BOOTSTRAP = (
    WORKSPACE_ROOT / "firm_command_center" / "eta_engine" / "deploy" / "vps_bootstrap.ps1"
)


def test_healthcheck_bootstrap_uses_canonical_script_remote_truth_and_repeating_once_trigger() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")

    assert '$healthScript = "$EtaEngineDir\\scripts\\health_check.py"' in text
    assert '$jarvisMemoryMigrationScript = "$EtaEngineDir\\scripts\\jarvis_memory_migration.py"' in text
    assert '$healthOutDir = "$InstallRoot\\firm_command_center\\var\\health"' in text
    assert "--allow-remote-supervisor-truth --allow-remote-retune-truth --output-dir" in text
    assert "$pythonExe $jarvisMemoryMigrationScript --apply --json" in text
    assert '$action = New-ScheduledTaskAction -Execute $pythonExe' in text
    assert "-Argument $healthArgs" in text
    assert (
        'New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval '
        '(New-TimeSpan -Hours 4) -RepetitionDuration (New-TimeSpan -Days 365)'
    ) in text
    assert (
        '& $pythonExe $healthScript --allow-remote-supervisor-truth '
        '--allow-remote-retune-truth --output-dir $healthOutDir'
    ) in text


def test_variant_bootstraps_keep_healthcheck_contract() -> None:
    for path in VARIANT_BOOTSTRAPS:
        text = path.read_text(encoding="utf-8")
        assert 'Join-Path $InstallRoot "eta_engine\\deploy\\vps_bootstrap.ps1"' in text, path.name
        assert "& $canonicalBootstrap" in text, path.name
        assert "ETA-HealthCheck" in text, path.name
        assert "--allow-remote-supervisor-truth --allow-remote-retune-truth --output-dir" in text, path.name
        assert (
            'New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval '
            '(New-TimeSpan -Hours 4) -RepetitionDuration (New-TimeSpan -Days 365)'
        ) in text, path.name
        assert (
            '& $pythonExe $healthScript --allow-remote-supervisor-truth '
            '--allow-remote-retune-truth --output-dir $healthOutDir'
        ) in text, path.name


def test_canonical_vps_fix_uses_remote_truth_healthcheck_contract() -> None:
    text = CANONICAL_VPS_FIX.read_text(encoding="utf-8")

    assert "workspace_roots.WORKSPACE_ROOT" in text
    assert 'HEALTH_SCRIPT = workspace_roots.ETA_ENGINE_ROOT / "scripts" / "health_check.py"' in text
    assert 'HEALTH_OUTPUT_DIR = workspace_roots.WORKSPACE_ROOT / "firm_command_center" / "var" / "health"' in text
    assert '"--allow-remote-supervisor-truth"' in text
    assert '"--allow-remote-retune-truth"' in text
    assert '"--output-dir"' in text
    assert '[str(python_exe), str(HEALTH_SCRIPT), *HEALTH_ARGS]' in text


def test_healthcheck_repair_script_matches_bootstrap_contract() -> None:
    text = REPAIR.read_text(encoding="utf-8")

    assert "[CmdletBinding()]" in text
    assert "param(" in text
    assert "[switch]$DryRun" in text
    assert '$taskName = "ETA-HealthCheck"' in text
    assert '$workspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert '$healthScript = Join-Path $workspaceRoot "eta_engine\\scripts\\health_check.py"' in text
    assert '$healthOutDir = Join-Path $workspaceRoot "firm_command_center\\var\\health"' in text
    assert "--allow-remote-supervisor-truth --allow-remote-retune-truth --output-dir" in text
    assert "if ($DryRun) {" in text
    assert "trigger = \"once_plus_4h_repeat_365d\"" in text
    assert "-WorkingDirectory $workspaceRoot" in text
    assert "Unregister-ScheduledTask -TaskName $taskName -Confirm:$false" in text
    assert "Start-ScheduledTask -TaskName $taskName" in text
    assert "Format-List Execute,Arguments,WorkingDirectory" in text


def test_healthcheck_script_docstring_uses_canonical_task_contract() -> None:
    text = HEALTH_CHECK_SCRIPT.read_text(encoding="utf-8")

    assert "Canonical task: ETA-HealthCheck" in text
    assert "--allow-remote-supervisor-truth" in text
    assert "--allow-remote-retune-truth" in text
    assert "firm_command_center\\\\var\\\\health" in text
    assert "ETA-VPS-HealthCheck" not in text


def test_legacy_healthcheck_bootstrap_is_wrapper_not_stale_task_logic() -> None:
    text = LEGACY_BOOTSTRAP.read_text(encoding="utf-8")

    assert '[CmdletBinding()]' in text
    assert 'Join-Path $InstallRoot "eta_engine\\deploy\\vps_bootstrap.ps1"' in text
    assert 'if ($entry.Key -eq "SkipKaizen") {' in text
    assert "Legacy lane notice: forwarding to canonical bootstrap" in text
    assert "& $canonicalBootstrap @forwardParams" in text
    assert "New-ScheduledTaskTrigger" not in text
    assert "Register-ScheduledTask" not in text


def test_scheduled_task_audit_flags_healthcheck_contract_drift() -> None:
    text = SCHEDULED_TASK_AUDIT.read_text(encoding="utf-8")

    assert '$expectedHealthCheckTokens = @(' in text
    assert '"ETA-HealthCheck"' in text
    assert "--allow-remote-supervisor-truth" in text
    assert "--allow-remote-retune-truth" in text
    assert "firm_command_center\\var\\health" in text
    assert "is_healthcheck_contract_drift" in text
    assert "healthcheck_contract_issue" in text
    assert '$needsAttention = (' in text
    assert "$healthCheckContractDrift -or" in text


def test_dashboard_durability_repair_includes_healthcheck_repair() -> None:
    text = REPAIR_DURABILITY.read_text(encoding="utf-8")

    assert 'set "REPAIR_HEALTHCHECK=%SCRIPTS%\\repair_eta_healthcheck_task.ps1"' in text
    assert 'File "%REPAIR_HEALTHCHECK%" -DryRun' in text
    assert 'File "%REPAIR_HEALTHCHECK%"' in text
    assert "validate canonical ETA-HealthCheck repair" in text
    assert "Repair canonical ETA-HealthCheck task" in text
    assert "including canonical ETA-HealthCheck repair" in text


def test_vps_ops_runbook_documents_healthcheck_repair_contract() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "repair_eta_healthcheck_task.ps1" in text
    assert "ETA-HealthCheck" in text
    assert "--allow-remote-supervisor-truth" in text
    assert "--allow-remote-retune-truth" in text
    assert "firm_command_center\\var\\health" in text
