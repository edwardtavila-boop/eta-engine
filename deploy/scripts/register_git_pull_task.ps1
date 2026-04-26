# Register the Apex-Git-Pull scheduled task on the VPS.
# Auto-pulls origin/ops/vps-24x7-stack into C:\eta_engine every 5 minutes
# so local C:\dev\eta_engine\ edits flow to the VPS automatically.
#
# Idempotent: re-running this script overwrites the existing task in place.
# Safe to run on a working VPS with the eta_engine repo already cloned.
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\eta_engine",
    [string]$Branch = "ops/vps-24x7-stack",
    [string]$LogDir = "$env:LOCALAPPDATA\eta_engine\logs"
)

$taskName = "Apex-Git-Pull"

if (-not (Test-Path $InstallDir)) {
    Write-Host "[git-pull-task] ERROR: $InstallDir does not exist on this machine." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $InstallDir ".git"))) {
    Write-Host "[git-pull-task] ERROR: $InstallDir is not a git repo." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# Build the cmd /c command line. Using cmd because PowerShell argument quoting
# inside scheduled tasks is fragile.
$logFile = Join-Path $LogDir "git_pull.log"
$cmdLine = "/c cd /d `"$InstallDir`" && git fetch origin && git reset --hard origin/$Branch >> `"$logFile`" 2>&1"

# Drop any existing task first so this script is idempotent.
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdLine -WorkingDirectory $InstallDir

$maxDur = (New-TimeSpan -Days 9999)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration $maxDur

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -User $env:USERNAME -RunLevel Limited | Out-Null

# Kick it off once immediately so the first pull happens now.
Start-ScheduledTask -TaskName $taskName

Write-Host "[ OK ] $taskName registered" -ForegroundColor Green
Write-Host "       branch:    origin/$Branch"
Write-Host "       working:   $InstallDir"
Write-Host "       log:       $logFile"
Write-Host "       interval:  5 minutes"
Write-Host ""
Write-Host "First run kicked off. Tail the log to verify:" -ForegroundColor Cyan
Write-Host "  Get-Content `"$logFile`" -Tail 20 -Wait"
