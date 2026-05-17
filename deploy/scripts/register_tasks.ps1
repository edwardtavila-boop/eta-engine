# Standalone task registration -- called separately from install_windows.ps1
# to avoid ErrorActionPreference issues with git.
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\EvolutionaryTradingAlgo\eta_engine",
    [string]$StateDir = "",
    [string]$LogDir = ""
)

$workspaceRoot = Split-Path -Parent $InstallDir
if (-not $StateDir) {
    $StateDir = Join-Path $workspaceRoot "var\eta_engine\state"
}
if (-not $LogDir) {
    $LogDir = Join-Path $workspaceRoot "logs\eta_engine"
}

$venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"

Write-Host "[ETA-tasks] InstallDir=$InstallDir" -ForegroundColor Cyan
Write-Host "[ETA-tasks] StateDir  =$StateDir"
Write-Host "[ETA-tasks] LogDir    =$LogDir"

# ----- 12 scheduled tasks ---------------------------------------------------
$tasks = @(
    @{ Name="ETA-Executor-DashboardAssemble";  Task="DASHBOARD_ASSEMBLE"; Trigger="MINUTELY" },
    @{ Name="ETA-Executor-LogCompact";         Task="LOG_COMPACT";        Trigger="HOURLY" },
    @{ Name="ETA-Executor-PromptWarmup";       Task="PROMPT_WARMUP";      Trigger="DAILY-1325" },
    @{ Name="ETA-Executor-AuditSummarize";     Task="AUDIT_SUMMARIZE";    Trigger="DAILY-0600" },
    @{ Name="ETA-Steward-ShadowTick";        Task="SHADOW_TICK";        Trigger="EVERY-5MIN" },
    @{ Name="ETA-Steward-DriftSummary";      Task="DRIFT_SUMMARY";      Trigger="EVERY-15MIN" },
    @{ Name="ETA-Steward-KaizenRetro";       Task="KAIZEN_RETRO";       Trigger="DAILY-2300" },
    @{ Name="ETA-Steward-DistillTrain";      Task="DISTILL_TRAIN";      Trigger="WEEKLY-SUN-0200" },
    @{ Name="ETA-Reasoner-TwinVerdict";       Task="TWIN_VERDICT";       Trigger="DAILY-2200" },
    @{ Name="ETA-Reasoner-StrategyMine";      Task="STRATEGY_MINE";      Trigger="WEEKLY-MON-0300" },
    @{ Name="ETA-Reasoner-CausalReview";      Task="CAUSAL_REVIEW";      Trigger="DAILY-0400" },
    @{ Name="ETA-Reasoner-DoctrineReview";    Task="DOCTRINE_REVIEW";    Trigger="DAILY-0500" },
    @{ Name="ETA-Steward-MetaUpgrade";        Task="META_UPGRADE";       Trigger="DAILY-0430" },
    @{ Name="ETA-Steward-HealthWatchdog";      Task="HEALTH_WATCHDOG";    Trigger="EVERY-5MIN" },
    @{ Name="ETA-Steward-SelfTest";            Task="SELF_TEST";          Trigger="DAILY-0300" },
    @{ Name="ETA-Executor-LogRotate";            Task="LOG_ROTATE";         Trigger="DAILY-0100" },
    @{ Name="ETA-Executor-DiskCleanup";          Task="DISK_CLEANUP";       Trigger="WEEKLY-SUN-0200" },
    @{ Name="ETA-Steward-Backup";              Task="BACKUP";             Trigger="DAILY-0500" },
    @{ Name="ETA-Executor-PrometheusExport";     Task="PROMETHEUS_EXPORT";  Trigger="MINUTELY" }
)

function New-ETATrigger([string]$Spec) {
    $maxDur = (New-TimeSpan -Days 9999)
    switch -Regex ($Spec) {
        "^MINUTELY$"          { return New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)  -RepetitionDuration $maxDur }
        "^EVERY-5MIN$"        { return New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)  -RepetitionDuration $maxDur }
        "^EVERY-15MIN$"       { return New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration $maxDur }
        "^HOURLY$"            { return New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)    -RepetitionDuration $maxDur }
        "^DAILY-(\d{2})(\d{2})$" {
            return New-ScheduledTaskTrigger -Daily -At (Get-Date -Hour ([int]$matches[1]) -Minute ([int]$matches[2]) -Second 0)
        }
        "^WEEKLY-(\w+)-(\d{2})(\d{2})$" {
            $m=@{"SUN"="Sunday";"MON"="Monday";"TUE"="Tuesday";"WED"="Wednesday";"THU"="Thursday";"FRI"="Friday";"SAT"="Saturday"}
            return New-ScheduledTaskTrigger -Weekly -DaysOfWeek $m[$matches[1]] -At (Get-Date -Hour ([int]$matches[2]) -Minute ([int]$matches[3]) -Second 0)
        }
    }
    throw "unknown trigger spec: $Spec"
}

foreach ($t in $tasks) {
    Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $venvPython `
        -Argument "-m deploy.scripts.run_task $($t.Task) --state-dir `"$StateDir`" --log-dir `"$LogDir`"" `
        -WorkingDirectory $InstallDir
    $trigger = New-ETATrigger -Spec $t.Trigger
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Settings $settings -User $env:USERNAME -RunLevel Limited | Out-Null
    Write-Host "[ OK ] $($t.Name) ($($t.Task))" -ForegroundColor Green
}

# ----- 3 boot-time services -------------------------------------------------
$bootTasks = @(
    @{ Name="ETA-Jarvis-Live";    Module="eta_engine.scripts.jarvis_live"; Args="--out-dir `"$StateDir`" --interval 60" },
    @{ Name="ETA-Avengers-Fleet"; Module="deploy.scripts.avengers_daemon";    Args="--state-dir `"$StateDir`" --log-dir `"$LogDir`"" },
    @{ Name="ETA-Dashboard";      Module="uvicorn";                           Args="deploy.scripts.dashboard_api:app --host 127.0.0.1 --port 8000" }
)

foreach ($t in $bootTasks) {
    Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $venvPython `
        -Argument "-m $($t.Module) $($t.Args)" -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Settings $settings -User $env:USERNAME -RunLevel Limited | Out-Null
    Write-Host "[ OK ] boot: $($t.Name)" -ForegroundColor Green
}

# ----- Kaizen daily elite-framework loop ------------------------------------
$kaizenTaskName = "ETA-Kaizen-Loop"
$kaizenScript = Join-Path $InstallDir "scripts\kaizen_loop.py"
if (Test-Path $kaizenScript) {
    Unregister-ScheduledTask -TaskName $kaizenTaskName -Confirm:$false -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $venvPython `
        -Argument "`"$kaizenScript`" --apply" -WorkingDirectory $workspaceRoot
    $trigger = New-ScheduledTaskTrigger -Daily -At "06:00"
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $kaizenTaskName -Action $action -Trigger $trigger `
        -Settings $settings -User $env:USERNAME -RunLevel Limited | Out-Null
    Write-Host "[ OK ] $kaizenTaskName (kaizen_loop.py --apply daily 06:00)" -ForegroundColor Green
} else {
    Write-Host "[WARN] $kaizenTaskName skipped; missing $kaizenScript" -ForegroundColor Yellow
}

Write-Host "[ETA-tasks] All persona, boot, and kaizen tasks registered." -ForegroundColor Cyan
