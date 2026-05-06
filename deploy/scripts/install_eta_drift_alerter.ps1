# EVOLUTIONARY TRADING ALGO // install_eta_drift_alerter.ps1
# Registers the ETA-DriftAlerter Scheduled Task that polls the canonical
# dashboard /api/live/per_bot_alpaca every 5 minutes and pings Telegram via
# the existing hermes_bridge whenever a bot's drift_alarm flag is true.
#
# Mirrors the pattern from register_dashboard_api_task.ps1 — SYSTEM principal,
# AtStartup + AtLogOn triggers, restart-on-failure 999 attempts at 1m delay.
# The action invokes deploy/scripts/run_drift_alerter_task.cmd.

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$TaskName = "ETA-DriftAlerter"
$WorkspaceRoot = "C:\EvolutionaryTradingAlgo"
$WorkingDir = Join-Path $WorkspaceRoot "eta_engine"
$StateDir = Join-Path $WorkspaceRoot "var\eta_engine\state"
$LogDir = Join-Path $WorkspaceRoot "logs\eta_engine"
$StdoutLog = Join-Path $LogDir "drift_alerter.stdout.log"
$StderrLog = Join-Path $LogDir "drift_alerter.stderr.log"
$Runner = Join-Path $WorkingDir "deploy\scripts\run_drift_alerter_task.cmd"

function Assert-CanonicalEtaPath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing non-canonical ETA path: $Path"
    }
}

Assert-CanonicalEtaPath -Path $WorkingDir
Assert-CanonicalEtaPath -Path $StateDir
Assert-CanonicalEtaPath -Path $LogDir
Assert-CanonicalEtaPath -Path $StdoutLog
Assert-CanonicalEtaPath -Path $StderrLog
Assert-CanonicalEtaPath -Path $Runner

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Missing drift alerter task runner: $Runner"
}

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Working dir : $WorkingDir"
    Write-Host "  Runner      : $Runner"
    Write-Host "  State dir   : $StateDir"
    Write-Host "  Stdout log  : $StdoutLog"
    Write-Host "  Stderr log  : $StderrLog"
    Write-Host "  Principal   : NT AUTHORITY\SYSTEM (Highest)"
    Write-Host "  Triggers    : AtStartup + AtLogOn"
    Write-Host "  Restart     : 999 attempts, 1-minute delay"
    exit 0
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Existing '$TaskName' task found; unregistering first."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force -Path $StateDir, $LogDir | Out-Null

# Kill any stragglers from a previous run before re-registering.
Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { ([string]$_.CommandLine) -match "eta_engine\.scripts\.drift_alarm_alerter|drift_alarm_alerter" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Get-CimInstance Win32_Process -Filter "name='cmd.exe'" -ErrorAction SilentlyContinue |
    Where-Object { ([string]$_.CommandLine) -match "run_drift_alerter_task\.cmd|drift_alarm_alerter" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$action = New-ScheduledTaskAction -Execute $Runner -WorkingDirectory $WorkingDir
$triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "ETA drift alarm Telegram alerter - polls 127.0.0.1:8000/api/live/per_bot_alpaca every 5 min, pings hermes_bridge on drift_alarm." `
    -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
    -Force | Out-Null

if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
}

Write-Host "OK: Registered '$TaskName' as SYSTEM, AtStartup+AtLogOn, restart-on-fail."
Write-Host "    Runner: $Runner"
Write-Host "    State : $(Join-Path $StateDir 'drift_alert_state.json')"
Write-Host "    Logs  : $StdoutLog"
Write-Host "            $StderrLog"
Write-Host "    Start now with:  Start-ScheduledTask -TaskName ETA-DriftAlerter"

