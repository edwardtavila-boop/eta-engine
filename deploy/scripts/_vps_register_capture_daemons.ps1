$ErrorActionPreference = "Stop"

$pythonPath = "C:\Program Files\Python312\python.exe"
$workspaceRoot = "C:\EvolutionaryTradingAlgo"
$user = "fxut9145410\trader"

Write-Host "--- Registering Phase 1 capture daemons on VPS ---"
Write-Host "  python : $pythonPath"
Write-Host "  cwd    : $workspaceRoot"
Write-Host "  user   : $user"
Write-Host ""

if (-not (Test-Path $pythonPath)) {
    throw "Python not found at $pythonPath"
}

# Common task settings: long-running, restart on failure, no time limit
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 2) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)  # 0 = unlimited

# Trigger: start at logon AND at boot (defense in depth)
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$triggerBoot = New-ScheduledTaskTrigger -AtStartup

foreach ($name in @("ETA-CaptureTicks", "ETA-CaptureDepth")) {
    $script = if ($name -eq "ETA-CaptureTicks") {
        "eta_engine.scripts.capture_tick_stream"
    } else {
        "eta_engine.scripts.capture_depth_snapshots"
    }

    Write-Host "TASK: $name"

    # Unregister if exists (idempotent)
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  unregistering existing task"
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }

    $action = New-ScheduledTaskAction `
        -Execute $pythonPath `
        -Argument "-m $script --port 4002" `
        -WorkingDirectory $workspaceRoot

    Register-ScheduledTask -TaskName $name `
        -Action $action `
        -Trigger @($triggerLogon, $triggerBoot) `
        -Settings $settings `
        -User $user `
        -RunLevel Limited `
        -Description "Phase 1 capture daemon: $script (auto-managed)" | Out-Null

    Write-Host "  registered"

    # Try to start now
    try {
        Start-ScheduledTask -TaskName $name
        Write-Host "  started"
    } catch {
        Write-Host "  WARN start failed: $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "--- Verification ---"
foreach ($name in @("ETA-CaptureTicks", "ETA-CaptureDepth")) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($t) {
        $info = $t | Get-ScheduledTaskInfo
        Write-Host "  $name : State=$($t.State), LastResult=0x$($info.LastTaskResult.ToString('X')), LastRun=$($info.LastRunTime)"
    } else {
        Write-Host "  $name : NOT FOUND (registration failed)"
    }
}
Write-Host ""
Write-Host "Capture daemons should now be writing to:"
Write-Host "  $workspaceRoot\mnq_data\ticks\<SYMBOL>_<YYYYMMDD>.jsonl"
Write-Host "  $workspaceRoot\mnq_data\depth\<SYMBOL>_<YYYYMMDD>.jsonl"
Write-Host ""
Write-Host "Verify capture in 60s with:"
Write-Host "  python -m eta_engine.scripts.capture_health_monitor"
