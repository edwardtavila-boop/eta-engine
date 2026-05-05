# Force-Multiplier Health Probe — Windows Task Scheduler installer
#
# Schedules the FM health probe to run every 4 hours and write a JSON
# snapshot to var/eta_engine/state/fm_health.json (canonical workspace
# path). Dashboards / on-call tools poll that file instead of running
# the probe synchronously.
#
# Run from an elevated PowerShell if you want it scoped to all users; a
# normal shell registers it for the current user, which is what we want
# since the OAuth tokens (claude / codex) are per-user keychain entries.
#
# Usage:
#   pwsh -File eta_engine/scripts/install_fm_health_task.ps1
#   pwsh -File eta_engine/scripts/install_fm_health_task.ps1 -Live
#   pwsh -File eta_engine/scripts/install_fm_health_task.ps1 -Uninstall

[CmdletBinding()]
param(
    [string]$TaskName = 'FM-HealthProbe',
    [string]$Workspace = 'C:\EvolutionaryTradingAlgo',
    [string]$PythonExe = 'python',
    [int]$IntervalHours = 4,
    [switch]$Live,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed scheduled task: $TaskName"
    } else {
        Write-Output "Task not found: $TaskName"
    }
    exit 0
}

# Verify the workspace exists before registering anything.
$probePath = Join-Path $Workspace 'eta_engine\scripts\force_multiplier_health.py'
if (-not (Test-Path $probePath)) {
    throw "Force-Multiplier health probe not found at $probePath. Set -Workspace if your repo is elsewhere."
}

$snapshotPath = Join-Path $Workspace 'var\eta_engine\state\fm_health.json'

# Build the command. Use --quiet so the task doesn't write console output;
# the JSON snapshot is the only artifact we care about.
$arguments = @(
    '-m', 'eta_engine.scripts.force_multiplier_health',
    '--quiet',
    '--json-out', "`"$snapshotPath`""
)
if ($Live) {
    $arguments += '--live'
}

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument ($arguments -join ' ') `
    -WorkingDirectory $Workspace

# Trigger: every $IntervalHours, indefinitely. Start 2 minutes from now so
# the first run picks up immediately (good for verifying the install).
$startTime = (Get-Date).AddMinutes(2)
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $startTime `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours) `
    -RepetitionDuration ([TimeSpan]::MaxValue)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType S4U `
    -RunLevel Limited

# Replace existing definition if present.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Force-Multiplier health probe (every $IntervalHours h). Writes $snapshotPath." `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal | Out-Null

Write-Output "Installed: $TaskName"
Write-Output "  Runs every $IntervalHours hour(s) starting $startTime"
Write-Output "  Live mode: $($Live.IsPresent)"
Write-Output "  Snapshot:  $snapshotPath"
Write-Output ""
Write-Output "Verify with:"
Write-Output "  Get-ScheduledTask -TaskName $TaskName"
Write-Output "  Start-ScheduledTask -TaskName $TaskName   # run once now"
Write-Output "  Get-Content $snapshotPath                 # read the snapshot"
