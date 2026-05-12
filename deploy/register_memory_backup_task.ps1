# Register the nightly Hermes-memory backup as a Windows scheduled task.
# Runs at 04:00 UTC daily and takes a SQLite online-backup snapshot of
# var/eta_engine/state/hermes_memory_store.db into a rolling window.
#
# Idempotent: re-running will not duplicate the task.

[CmdletBinding()]
param(
    [string]$TaskName = "ETA-Hermes-Memory-Backup",
    [string]$PythonExe = "C:\Users\Administrator\.hermes\hermes-agent\.venv\Scripts\python.exe",
    [string]$ScriptModule = "eta_engine.scripts.hermes_memory_backup",
    [string]$RunTime = "04:00",
    [string]$WorkspaceRoot = "C:\EvolutionaryTradingAlgo"
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python venv not found at $PythonExe - adjust -PythonExe arg"
    exit 1
}

# Build the action with PYTHONPATH set so the module resolves without a .pth file.
$LogPath = Join-Path $WorkspaceRoot "var\hermes_memory_backup.log"
$cmdLine = '/c set "PYTHONPATH={0}" && "{1}" -m {2} --quiet >> "{3}" 2>&1' -f `
    $WorkspaceRoot, $PythonExe, $ScriptModule, $LogPath

$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument $cmdLine

$trigger = New-ScheduledTaskTrigger -Daily -At $RunTime

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Output "Existing task '$TaskName' found - unregistering before re-registering"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Nightly online-backup of Hermes operator memory SQLite DB. Keeps 14 rolling snapshots."

Write-Output "Registered scheduled task '$TaskName' to run daily at $RunTime"
Write-Output "Log file: $LogPath"
Write-Output ""
Write-Output "To test immediately:"
Write-Output "  schtasks /Run /TN $TaskName"
Write-Output ""
Write-Output "To inspect:"
Write-Output "  Get-ScheduledTaskInfo -TaskName $TaskName"
