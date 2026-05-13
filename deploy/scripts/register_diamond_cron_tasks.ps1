# EVOLUTIONARY TRADING ALGO // deploy/scripts/register_diamond_cron_tasks.ps1
# ===========================================================================
# Register the wave-13 through wave-19 diamond program scheduled tasks on
# the VPS. Closes the operator-ask #2 ("VPS closed_trade_ledger cron")
# from the post-wave-19 punch list -- the watchdog finally has data to
# classify, and the leaderboard finally populates PROP_READY_BOTS so the
# capital_allocator can route real-fund tier capital to the elite-3.
#
# 15-min cadence:
#   ETA-Diamond-LedgerEvery15Min       - closed_trade_ledger.py refresh
#                                         (feeds every other diamond audit)
#
# Hourly cadence:
#   ETA-Diamond-LeaderboardHourly      - composite scoring + PROP_READY top-3
#   ETA-Diamond-OpsDashboardHourly     - unified status surface
#   ETA-Diamond-FeedSanityHourly       - STUCK_PRICE + ZERO_PNL detection
#
# Daily cadence (06:00 ET = 11:00 UTC):
#   ETA-Diamond-PromotionGateDaily     - promotion eligibility per bot
#   ETA-Diamond-SizingAuditDaily       - $/R per bot vs USD floor
#   ETA-Diamond-DirectionStratifyDaily - per-side R-edge analyzer
#   ETA-Diamond-DemotionGateDaily      - retire-recommendation advisory
#
# Idempotent: re-running re-registers each task without errors.
# Run on VPS as administrator after pulling latest eta_engine commits.

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

Write-Host "--- Registering diamond program cron tasks ---"
Write-Host "  python : $PythonPath"
Write-Host "  cwd    : $WorkspaceRoot"
Write-Host "  user   : $TaskUser"
Write-Host ""

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Use the SYSTEM service account (matches the existing
# ETA-Diamond-WatchdogDaily and other ETA-Diamond-* tasks).
# WORKGROUP\trader fails account-name-to-SID lookup on this VPS.
$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Limited

function Register-DiamondTask {
    param(
        [string]$Name,
        [string]$TaskArgs,
        [string]$Desc,
        [object]$Trigger
    )
    Write-Host "TASK: $Name"
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  unregistering existing task"
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }
    $action = New-ScheduledTaskAction `
        -Execute $PythonPath `
        -Argument $TaskArgs `
        -WorkingDirectory $WorkspaceRoot
    Register-ScheduledTask -TaskName $Name `
        -Action $action `
        -Trigger $Trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $Desc | Out-Null
    Write-Host "  registered (SYSTEM)"
    if ($StartNow) {
        Start-ScheduledTask -TaskName $Name
        Write-Host "  started"
    }
}

# -- 15-MIN CADENCE -- the ledger refresh that feeds every other audit --
# Windows Task Scheduler rejects RepetitionDuration > 31 days; use 365 days
# and rely on the operator to re-run this script annually (or wire into
# the existing yearly maintenance pass).
$every15Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 365)

$every15MinTasks = @(
    @{ Name = "ETA-Diamond-LedgerEvery15Min"
        Args = "-m eta_engine.scripts.closed_trade_ledger"
        Desc = "Diamond: refresh closed_trade_ledger_latest.json every 15 min so the watchdog + downstream audits have live data" }
)

foreach ($t in $every15MinTasks) {
    Register-DiamondTask -Name $t.Name -TaskArgs $t.Args -Desc $t.Desc -Trigger $every15Trigger
}

# -- HOURLY CADENCE -- leaderboard + ops dashboard + feed sanity --
$hourlyTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 365)

$hourlyTasks = @(
    @{ Name = "ETA-Diamond-LeaderboardHourly"
        Args = "-m eta_engine.scripts.diamond_leaderboard"
        Desc = "Diamond: composite scoring + PROP_READY top-3 designation (capital_allocator reads the receipt)" }
    @{ Name = "ETA-Diamond-OpsDashboardHourly"
        Args = "-m eta_engine.scripts.diamond_ops_dashboard"
        Desc = "Diamond: unified ops status (joins promotion + sizing + watchdog + direction + feed-sanity)" }
    @{ Name = "ETA-Diamond-FeedSanityHourly"
        Args = "-m eta_engine.scripts.diamond_feed_sanity_audit"
        Desc = "Diamond: detect STUCK_PRICE / ZERO_PNL_ACTIVITY / MISSING_PNL/SIDE_FIELD pollution (catches MBT-style bugs)" }
)

foreach ($t in $hourlyTasks) {
    Register-DiamondTask -Name $t.Name -TaskArgs $t.Args -Desc $t.Desc -Trigger $hourlyTrigger
}

# -- DAILY CADENCE -- promotion + sizing + direction + demotion advisories --
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At "11:00 AM"

$dailyTasks = @(
    @{ Name = "ETA-Diamond-PromotionGateDaily"
        Args = "-m eta_engine.scripts.diamond_promotion_gate --include-existing"
        Desc = "Diamond: PROMOTE/NEEDS_MORE_DATA/REJECT verdict per candidate against the 5 hard + 5 soft gates" }
    @{ Name = "ETA-Diamond-SizingAuditDaily"
        Args = "-m eta_engine.scripts.diamond_sizing_audit"
        Desc = "Diamond: USD-per-R sizing classification (SIZING_OK/TIGHT/FRAGILE/BREACHED)" }
    @{ Name = "ETA-Diamond-DirectionStratifyDaily"
        Args = "-m eta_engine.scripts.diamond_direction_stratify"
        Desc = "Diamond: per-side R-edge analyzer (SYMMETRIC/LONG_DOMINANT/SHORT_DOMINANT/...)" }
    @{ Name = "ETA-Diamond-DemotionGateDaily"
        Args = "-m eta_engine.scripts.diamond_demotion_gate"
        Desc = "Diamond: KEEP/WATCH/DEMOTE_CANDIDATE recommendation (advisory only -- never auto-mutates DIAMOND_BOTS)" }
)

foreach ($t in $dailyTasks) {
    Register-DiamondTask -Name $t.Name -TaskArgs $t.Args -Desc $t.Desc -Trigger $dailyTrigger
}

Write-Host ""
Write-Host "--- Done. Verify with: schtasks /query /fo csv /nh ^| findstr Diamond ---"
