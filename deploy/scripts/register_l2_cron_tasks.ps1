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

param(
    [string]$PythonPath = "",
    [string]$WorkspaceRoot = "C:\EvolutionaryTradingAlgo",
    [string]$TaskUser = "",
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
    $PythonPath = "C:\Program Files\Python312\python.exe"
}

if (-not $TaskUser) {
    $TaskUser = [Environment]::GetEnvironmentVariable("ETA_TASK_USER", "Machine")
}
if (-not $TaskUser) {
    $TaskUser = [Environment]::GetEnvironmentVariable("ETA_TASK_USER", "User")
}
if (-not $TaskUser) {
    $TaskUser = "$env:USERDOMAIN\$env:USERNAME"
}

if (-not (Test-Path $PythonPath)) {
    throw "Python not found at $PythonPath"
}
if (-not (Test-Path $WorkspaceRoot)) {
    throw "Workspace root not found at $WorkspaceRoot"
}

Write-Host "--- Registering L2 supercharge cron tasks ---"
Write-Host "  python : $PythonPath"
Write-Host "  cwd    : $WorkspaceRoot"
Write-Host "  user   : $TaskUser"
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
    @{ Name = "ETA-L2-DriftMonitor"
        Args = "-m eta_engine.scripts.l2_drift_monitor"
        Desc = "L2: rolling-window performance drift detector" }
    @{ Name = "ETA-L2-RiskMetrics"
        Args = "-m eta_engine.scripts.l2_risk_metrics --days 30"
        Desc = "L2: Sortino + Calmar + daily P&L rollup" }
    @{ Name = "ETA-Diamond-WatchdogDaily"
        Args = "-m eta_engine.scripts.diamond_falsification_watchdog"
        Desc = "Diamond: daily falsification watchdog (buffer-to-retirement metric)" }
)

foreach ($t in $dailyTasks) {
    Write-Host "TASK: $($t.Name)"
    $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  unregistering existing task"
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
    }
    $action = New-ScheduledTaskAction `
        -Execute $PythonPath `
        -Argument $t.Args `
        -WorkingDirectory $WorkspaceRoot
    Register-ScheduledTask -TaskName $t.Name `
        -Action $action `
        -Trigger $dailyTrigger `
        -Settings $settings `
        -User $TaskUser `
        -RunLevel Limited `
        -Description $t.Desc | Out-Null
    Write-Host "  registered (daily 11:00 UTC)"
    if ($StartNow) {
        Start-ScheduledTask -TaskName $t.Name
        Write-Host "  started"
    }
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
    @{ Name = "ETA-L2-SlipRetrainWeekly"
        Args = "-m eta_engine.scripts.l2_slippage_predictor --train --days 60"
        Desc = "L2: weekly slip-prediction model retrain on last 60d fills" }
    @{ Name = "ETA-L2-FillLatencyWeekly"
        Args = "-m eta_engine.scripts.l2_fill_latency --days 7"
        Desc = "L2: weekly signal-to-fill latency vs decay window" }
    @{ Name = "ETA-L2-CorrelationWeekly"
        Args = "-m eta_engine.scripts.l2_strategy_correlation --days 60"
        Desc = "L2: weekly cross-strategy correlation tracker" }
    @{ Name = "ETA-L2-EnsembleValidatorWeekly"
        Args = "-m eta_engine.scripts.l2_ensemble_validator --days 30"
        Desc = "L2: weekly check that ensemble beats best individual" }
    @{ Name = "ETA-L2-UniverseAuditWeekly"
        Args = "-m eta_engine.scripts.l2_universe_audit --days 90"
        Desc = "L2: weekly survivorship-bias check on backtest universe" }
    @{ Name = "ETA-L2-CommissionTierWeekly"
        Args = "-m eta_engine.scripts.l2_commission_tier_optimizer --days 30"
        Desc = "L2: weekly IBKR commission tier projection" }
    @{ Name = "ETA-Diamond-AuthenticityWeekly"
        Args = "-m eta_engine.scripts.diamond_authenticity_audit"
        Desc = "Diamond: weekly authenticity audit (GENUINE/LAB/CZ verdict per diamond)" }
)

foreach ($t in $weeklyTasks) {
    Write-Host "TASK: $($t.Name)"
    $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  unregistering existing task"
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
    }
    $action = New-ScheduledTaskAction `
        -Execute $PythonPath `
        -Argument $t.Args `
        -WorkingDirectory $WorkspaceRoot
    Register-ScheduledTask -TaskName $t.Name `
        -Action $action `
        -Trigger $weeklyTrigger `
        -Settings $settings `
        -User $TaskUser `
        -RunLevel Limited `
        -Description $t.Desc | Out-Null
    Write-Host "  registered (weekly Sun 11:00 UTC)"
    if ($StartNow) {
        Start-ScheduledTask -TaskName $t.Name
        Write-Host "  started"
    }
}

Write-Host ""
Write-Host "--- Verification ---"
Get-ScheduledTask -TaskName "ETA-L2-*" -ErrorAction SilentlyContinue |
    Select-Object TaskName, State |
    Format-Table -AutoSize | Out-String | Write-Host

Write-Host "Done.  Run any task manually with Start-ScheduledTask -TaskName <name>"
