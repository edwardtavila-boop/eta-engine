# Standalone task registration -- called separately from install_windows.ps1
# to avoid ErrorActionPreference issues with git.
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\apex_predator",
    [string]$StateDir = "$env:LOCALAPPDATA\apex_predator\state",
    [string]$LogDir = "$env:LOCALAPPDATA\apex_predator\logs"
)

$venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"

Write-Host "[apex-tasks] InstallDir=$InstallDir" -ForegroundColor Cyan
Write-Host "[apex-tasks] StateDir  =$StateDir"
Write-Host "[apex-tasks] LogDir    =$LogDir"

# ----- 12 scheduled tasks ---------------------------------------------------
$tasks = @(
    @{ Name="Apex-Robin-DashboardAssemble";  Task="DASHBOARD_ASSEMBLE"; Trigger="MINUTELY" },
    @{ Name="Apex-Robin-LogCompact";         Task="LOG_COMPACT";        Trigger="HOURLY" },
    @{ Name="Apex-Robin-PromptWarmup";       Task="PROMPT_WARMUP";      Trigger="DAILY-1325" },
    @{ Name="Apex-Robin-AuditSummarize";     Task="AUDIT_SUMMARIZE";    Trigger="DAILY-0600" },
    @{ Name="Apex-Alfred-ShadowTick";        Task="SHADOW_TICK";        Trigger="EVERY-5MIN" },
    @{ Name="Apex-Alfred-DriftSummary";      Task="DRIFT_SUMMARY";      Trigger="EVERY-15MIN" },
    @{ Name="Apex-Alfred-KaizenRetro";       Task="KAIZEN_RETRO";       Trigger="DAILY-2300" },
    @{ Name="Apex-Alfred-DistillTrain";      Task="DISTILL_TRAIN";      Trigger="WEEKLY-SUN-0200" },
    @{ Name="Apex-Batman-TwinVerdict";       Task="TWIN_VERDICT";       Trigger="DAILY-2200" },
    @{ Name="Apex-Batman-StrategyMine";      Task="STRATEGY_MINE";      Trigger="WEEKLY-MON-0300" },
    @{ Name="Apex-Batman-CausalReview";      Task="CAUSAL_REVIEW";      Trigger="DAILY-0400" },
    @{ Name="Apex-Batman-DoctrineReview";    Task="DOCTRINE_REVIEW";    Trigger="DAILY-0500" },
    @{ Name="Apex-Alfred-MetaUpgrade";        Task="META_UPGRADE";       Trigger="DAILY-0430" }
)

function New-ApexTrigger([string]$Spec) {
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
    $trigger = New-ApexTrigger -Spec $t.Trigger
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Settings $settings -User $env:USERNAME -RunLevel Limited | Out-Null
    Write-Host "[ OK ] $($t.Name) ($($t.Task))" -ForegroundColor Green
}

# ----- 3 boot-time services -------------------------------------------------
$bootTasks = @(
    @{ Name="Apex-Jarvis-Live";    Module="apex_predator.scripts.jarvis_live"; Args="--inputs docs\premarket_inputs.json --out-dir `"$StateDir`" --interval 60" },
    @{ Name="Apex-Avengers-Fleet"; Module="deploy.scripts.avengers_daemon";    Args="--state-dir `"$StateDir`" --log-dir `"$LogDir`"" },
    @{ Name="Apex-Dashboard";      Module="uvicorn";                           Args="deploy.scripts.dashboard_api:app --host 127.0.0.1 --port 8000" }
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

Write-Host "[apex-tasks] All 15 tasks registered." -ForegroundColor Cyan
