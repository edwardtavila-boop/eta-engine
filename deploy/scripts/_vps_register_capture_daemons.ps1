param(
    [string]$PythonPath = "",
    [string]$WorkspaceRoot = "C:\EvolutionaryTradingAlgo",
    [string]$TaskUser = "",
    [string[]]$TickSymbols = @("MNQ", "NQ", "M2K", "6E", "MCL"),
    [string[]]$DepthSymbols = @("MNQ", "NQ", "ES", "MES", "YM", "MYM", "M2K"),
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"

if (-not $PythonPath) {
    $PythonPath = [Environment]::GetEnvironmentVariable("ETA_PYTHON_EXE", "Machine")
}
if (-not $PythonPath) {
    $PythonPath = [Environment]::GetEnvironmentVariable("ETA_PYTHON_EXE", "User")
}
if (-not $PythonPath) {
    $VenvPython = Join-Path $WorkspaceRoot "eta_engine\.venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $PythonPath = $VenvPython
    } else {
        $PythonPath = "C:\Program Files\Python312\python.exe"
    }
}

if (-not $TaskUser) {
    $TaskUser = [Environment]::GetEnvironmentVariable("ETA_TASK_USER", "Machine")
}
if (-not $TaskUser) {
    $TaskUser = [Environment]::GetEnvironmentVariable("ETA_TASK_USER", "User")
}
if (-not $TaskUser) {
    $TaskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}
if (-not $TaskUser) {
    $TaskUser = "$env:COMPUTERNAME\$env:USERNAME"
}

Write-Host "--- Registering Phase 1 capture daemons on VPS ---"
Write-Host "  python : $PythonPath"
Write-Host "  cwd    : $WorkspaceRoot"
Write-Host "  user   : $TaskUser"
Write-Host "  ticks  : $($TickSymbols -join ' ')"
Write-Host "  depth  : $($DepthSymbols -join ' ')"
Write-Host ""

if (-not (Test-Path $PythonPath)) {
    throw "Python not found at $PythonPath"
}
if (-not (Test-Path $WorkspaceRoot)) {
    throw "Workspace root not found at $WorkspaceRoot"
}

# Kill stale/manual capture workers so the fresh task registration does not
# inherit duplicate IBKR sessions or burn the 3-book depth budget.
$staleCaptureWorkers = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match '^python(\.exe)?$' -and
    $_.CommandLine -and
    (
        $_.CommandLine -match 'eta_engine\.scripts\.capture_tick_stream' -or
        $_.CommandLine -match 'eta_engine\.scripts\.capture_depth_snapshots'
    )
}
foreach ($worker in $staleCaptureWorkers) {
    try {
        Write-Host "  stopping stale worker PID $($worker.ProcessId): $($worker.CommandLine)"
        Stop-Process -Id $worker.ProcessId -Force -ErrorAction Stop
    } catch {
        Write-Host "  WARN could not stop stale worker PID $($worker.ProcessId): $($_.Exception.Message)"
    }
}

# Common task settings: long-running, restart on failure, no time limit
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 2) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)  # 0 = unlimited

# Trigger: start at logon AND at boot (defense in depth)
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser
$triggerBoot = New-ScheduledTaskTrigger -AtStartup

foreach ($name in @("ETA-CaptureTicks", "ETA-CaptureDepth")) {
    $script = if ($name -eq "ETA-CaptureTicks") {
        "eta_engine.scripts.capture_tick_stream"
    } else {
        "eta_engine.scripts.capture_depth_snapshots"
    }
    $symbols = if ($name -eq "ETA-CaptureTicks") { $TickSymbols } else { $DepthSymbols }
    $symbolArgs = ($symbols | ForEach-Object { $_.Trim() } | Where-Object { $_ }) -join " "
    $extraArgs = if ($name -eq "ETA-CaptureDepth") {
        "--max-active-depth-requests 3 --rotation-seconds 20"
    } else {
        ""
    }
    $taskArgs = "-m $script --port 4002 --symbols $symbolArgs"
    if ($extraArgs) {
        $taskArgs = "$taskArgs $extraArgs"
    }

    Write-Host "TASK: $name"

    # Unregister if exists (idempotent)
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  unregistering existing task"
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }

    $action = New-ScheduledTaskAction `
        -Execute $PythonPath `
        -Argument $taskArgs `
        -WorkingDirectory $WorkspaceRoot

    Register-ScheduledTask -TaskName $name `
        -Action $action `
        -Trigger @($triggerLogon, $triggerBoot) `
        -Settings $settings `
        -User $TaskUser `
        -RunLevel Limited `
        -Description "Phase 1 capture daemon: $script (auto-managed)" | Out-Null

    Write-Host "  registered"

    # Try to start now
    if ($StartNow) {
        try {
            Start-ScheduledTask -TaskName $name
            Write-Host "  started"
        } catch {
            Write-Host "  WARN start failed: $($_.Exception.Message)"
        }
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
Write-Host "  $WorkspaceRoot\mnq_data\ticks\<SYMBOL>_<YYYYMMDD>.jsonl"
Write-Host "  $WorkspaceRoot\mnq_data\depth\<SYMBOL>_<YYYYMMDD>.jsonl"
Write-Host ""
Write-Host "Verify capture in 60s with:"
Write-Host "  python -m eta_engine.scripts.capture_health_monitor"
