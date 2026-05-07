# EVOLUTIONARY TRADING ALGO // register_kaizen_loop_task.ps1
# Registers the daily kaizen loop -- elite_scoreboard + monte_carlo +
# sage_oracle + bot_pressure + edge_tracker on autopilot. With --apply
# the task auto-deactivates bots that hit RETIRE for two consecutive
# runs (the 2-run confirmation gate inside kaizen_loop.run_loop).
#
# Modeled on register_paper_live_transition_check_task.ps1; same SYSTEM
# principal + canonical-path guards.

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$TaskName = "ETA-Kaizen-Loop"
$WorkspaceRoot = "C:\EvolutionaryTradingAlgo"
$WorkingDir = Join-Path $WorkspaceRoot "eta_engine"
$StateDir = Join-Path $WorkspaceRoot "var\eta_engine\state"
$LogDir = Join-Path $WorkspaceRoot "logs\eta_engine"
$Runner = Join-Path $WorkingDir "deploy\scripts\run_kaizen_loop_task.cmd"
$ReportsDir = Join-Path $StateDir "kaizen_reports"
$VenvPython = Join-Path $WorkingDir ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { "python.exe" }

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
Assert-CanonicalEtaPath -Path $Runner
Assert-CanonicalEtaPath -Path $ReportsDir

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Missing kaizen-loop task runner: $Runner"
}

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Working dir : $WorkingDir"
    Write-Host "  Python      : $PythonExe"
    Write-Host "  Runner      : $Runner"
    Write-Host "  Reports     : $ReportsDir"
    Write-Host "  State dir   : $StateDir"
    Write-Host "  Log dir     : $LogDir"
    Write-Host "  Principal   : NT AUTHORITY\SYSTEM (Highest)"
    Write-Host "  Trigger     : DAILY 06:00 UTC (server-local time, see task)"
    Write-Host "  Restart     : 3 attempts, 1-minute delay"
    exit 0
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Existing '$TaskName' task found; unregistering first."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force -Path $StateDir, $LogDir, $ReportsDir | Out-Null

# Daily 06:00 server-local. The kaizen loop's 2-run gate means each
# RETIRE recommendation needs two consecutive days of agreement before
# auto-deactivation, so the daily cadence is the natural beat.
$action = New-ScheduledTaskAction -Execute $Runner -WorkingDirectory $WorkspaceRoot
$trigger = New-ScheduledTaskTrigger -Daily -At "06:00"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Daily kaizen loop with --apply (elite_scoreboard + Monte Carlo + sage + edge_tracker; auto-deactivates bots that hit RETIRE for two consecutive runs)." `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Force | Out-Null

if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
}

Write-Host "OK: Registered '$TaskName' as SYSTEM, daily 06:00."
Write-Host "    Runner:   $Runner"
Write-Host "    Reports:  $ReportsDir"
Write-Host "    Logs:     $LogDir"
Write-Host "    Action history: $StateDir\kaizen_actions.jsonl"
Write-Host "    Start now with:  Start-ScheduledTask -TaskName '$TaskName'"
