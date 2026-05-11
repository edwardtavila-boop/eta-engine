# EVOLUTIONARY TRADING ALGO // deploy/scripts/register_l2_cron_tasks.ps1
# ========================================================================
# Register the L2 supercharge daily/weekly scheduled tasks on the VPS.
#
# Daily (06:00 ET = 11:00 UTC):
#   ETA-L2-BacktestDaily        - replay yesterday's depth through all 3 strategies
#   ETA-L2-PromotionEvaluator   - emit per-strategy verdict
#   ETA-L2-CalibrationDaily     - Brier score on confidence
#   ETA-L2-RegistryAdapter      - sync L2 verdicts to verdict_cache.json
#
# Weekly (Sunday 06:00 ET):
#   ETA-L2-SweepWeekly          - multi-config grid sweep with deflated sharpe
#   ETA-L2-FillAuditWeekly      - realized vs predicted slip
#
# Run on VPS after subscriptions are active and capture data is flowing.
# Idempotent: re-running re-registers each task without errors.

$ErrorActionPreference = "Stop"

$pythonPath = "C:\Program Files\Python312\python.exe"
$workspaceRoot = "C:\EvolutionaryTradingAlgo"
$user = "fxut9145410\trader"

if (-not (Test-Path $pythonPath)) {
    throw "Python not found at $pythonPath"
}

Write-Host "--- Registering L2 supercharge cron tasks ---"
Write-Host "  python : $pythonPath"
Write-Host "  cwd    : $workspaceRoot"
Write-Host "  user   : $user"
Write-Host ""

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

# Daily tasks at 06:00 ET (11:00 UTC)
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At "11:00 AM"

$dailyTasks = @(
    @{ Name = "ETA-L2-BacktestDaily"
        Args = "-m eta_engine.scripts.l2_backtest_harness --strategy book_imbalance --symbol MNQ --days 1"
        Desc = "L2: daily backtest replay of book_imbalance over yesterday's depth" }
    @{ Name = "ETA-L2-PromotionEvaluator"
        Args = "-m eta_engine.scripts.l2_promotion_evaluator"
        Desc = "L2: daily promotion verdict per strategy" }
    @{ Name = "ETA-L2-CalibrationDaily"
        Args = "-m eta_engine.scripts.l2_confidence_calibration"
        Desc = "L2: daily Brier score on confidence calibration" }
    @{ Name = "ETA-L2-RegistryAdapter"
        Args = "-m eta_engine.strategies.l2_registry_adapter"
        Desc = "L2: sync promotion verdicts to verdict_cache.json" }
)

foreach ($t in $dailyTasks) {
    Write-Host "TASK: $($t.Name)"
    $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  unregistering existing task"
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
    }
    $action = New-ScheduledTaskAction `
        -Execute $pythonPath `
        -Argument $t.Args `
        -WorkingDirectory $workspaceRoot
    Register-ScheduledTask -TaskName $t.Name `
        -Action $action `
        -Trigger $dailyTrigger `
        -Settings $settings `
        -User $user `
        -RunLevel Limited `
        -Description $t.Desc | Out-Null
    Write-Host "  registered (daily 11:00 UTC)"
}

# Weekly tasks: Sunday 06:00 ET (11:00 UTC)
$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "11:00 AM"

$weeklyTasks = @(
    @{ Name = "ETA-L2-SweepWeekly"
        Args = "-m eta_engine.scripts.l2_sweep_harness --symbol MNQ --days 14"
        Desc = "L2: weekly multi-config sweep with deflated sharpe" }
    @{ Name = "ETA-L2-FillAuditWeekly"
        Args = "-m eta_engine.scripts.l2_fill_audit --days 7"
        Desc = "L2: weekly realized-vs-predicted slip audit per session" }
)

foreach ($t in $weeklyTasks) {
    Write-Host "TASK: $($t.Name)"
    $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  unregistering existing task"
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
    }
    $action = New-ScheduledTaskAction `
        -Execute $pythonPath `
        -Argument $t.Args `
        -WorkingDirectory $workspaceRoot
    Register-ScheduledTask -TaskName $t.Name `
        -Action $action `
        -Trigger $weeklyTrigger `
        -Settings $settings `
        -User $user `
        -RunLevel Limited `
        -Description $t.Desc | Out-Null
    Write-Host "  registered (weekly Sun 11:00 UTC)"
}

Write-Host ""
Write-Host "--- Verification ---"
Get-ScheduledTask -TaskName "ETA-L2-*" -ErrorAction SilentlyContinue |
    Select-Object TaskName, State |
    Format-Table -AutoSize | Out-String | Write-Host

Write-Host "Done.  Run any task manually with Start-ScheduledTask -TaskName <name>"
