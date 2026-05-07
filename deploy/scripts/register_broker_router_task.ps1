# EVOLUTIONARY TRADING ALGO // register_broker_router_task.ps1
# Register the broker-router service as a Windows Scheduled Task on
# the VPS. The router consumes <bot_id>.pending_order.json files and
# dispatches them through the SmartRouter (closes the gap documented
# in docs/PAPER_LIVE_ROUTING_GAP.md, option 3).
#
# Runs as NT AUTHORITY\SYSTEM (survives logout); triggers AtStartup
# AND AtLogOn; restarts on failure. Idempotent.
#
# Usage (run elevated):
#   powershell.exe -ExecutionPolicy Bypass -File `
#     C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\register_broker_router_task.ps1

[CmdletBinding()]
param([switch]$DryRun)

$ErrorActionPreference = "Stop"

$TaskName   = "ETA-Broker-Router"
$WorkingDir = "C:\EvolutionaryTradingAlgo\eta_engine"
$Runner     = Join-Path $WorkingDir "deploy\scripts\run_broker_router_task.cmd"
$VenvPython = Join-Path $WorkingDir ".venv\Scripts\python.exe"
$PythonExe  = if (Test-Path $VenvPython) { $VenvPython } else { "python.exe" }

$EnvVars = @{
    "ETA_BROKER_ROUTER_INTERVAL_S"  = "5"
    "ETA_BROKER_ROUTER_PENDING_DIR" = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\router\pending"
    "ETA_BROKER_ROUTER_STATE_ROOT"  = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\router"
    "ETA_BROKER_ROUTER_ENFORCE_READINESS" = "1"
}

# Windows scheduled tasks have no native env-var block, so set them
# inside cmd /c (matches register_fleet_tasks.ps1 style).
$envPrefix = ""
foreach ($key in $EnvVars.Keys) {
    $envPrefix += "set ""$key=$($EnvVars[$key])"" && "
}
$cmdLine = "/c $envPrefix cd /d ""$WorkingDir"" && ""$PythonExe"" -m eta_engine.scripts.broker_router"

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Working dir : $WorkingDir"
    Write-Host "  Runner      : $Runner"
    Write-Host "  Python      : $PythonExe (selected by runner)"
    Write-Host "  Legacy cmd  : cmd.exe $cmdLine"
    Write-Host "  Principal   : NT AUTHORITY\SYSTEM (Highest)"
    Write-Host "  Triggers    : AtStartup + AtLogOn"
    Write-Host "  Restart     : 3 attempts, 1-minute delay"
    exit 0
}

# Idempotency: drop any prior registration before re-creating.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Existing '$TaskName' task found; unregistering first."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction -Execute $Runner -WorkingDirectory $WorkingDir
$triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)
$principal = New-ScheduledTaskPrincipal `
    -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Broker-router service: consumes <bot_id>.pending_order.json files and dispatches via SmartRouter. Closes paper-live routing gap." `
    -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
    -Force | Out-Null

Write-Host "OK: Registered '$TaskName' as SYSTEM, AtStartup+AtLogOn, restart-on-fail."
Write-Host "    Start now with:  Start-ScheduledTask -TaskName '$TaskName'"
