# EVOLUTIONARY TRADING ALGO // register_dashboard_diagnostics_cache_warm_task.ps1
# Registers a bounded read-only warmer for the dashboard diagnostics cache.

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$TaskName = "ETA-DashboardDiagnosticsCacheWarm"
$WorkspaceRoot = "C:\EvolutionaryTradingAlgo"
$WorkingDir = Join-Path $WorkspaceRoot "eta_engine"
$LogDir = Join-Path $WorkspaceRoot "logs\eta_engine"
$Runner = Join-Path $WorkingDir "deploy\scripts\run_dashboard_diagnostics_cache_warm.ps1"
$Url = "http://127.0.0.1:8421/api/dashboard/diagnostics?refresh=1"

function Assert-CanonicalEtaPath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing non-canonical ETA path: $Path"
    }
}

Assert-CanonicalEtaPath -Path $WorkingDir
Assert-CanonicalEtaPath -Path $LogDir
Assert-CanonicalEtaPath -Path $Runner

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Missing dashboard diagnostics cache warmer: $Runner"
}

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Working dir : $WorkingDir"
    Write-Host "  Runner      : $Runner"
    Write-Host "  URL         : $Url"
    Write-Host "  Log dir     : $LogDir"
    Write-Host "  Principal   : NT AUTHORITY\SYSTEM (Highest)"
    Write-Host "  Triggers    : AtStartup + AtLogOn + every minute"
    Write-Host "  Runtime     : 3 local refreshes, 20 seconds apart"
    Write-Host "  Order action: never submits, cancels, flattens, or promotes"
    exit 0
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Existing '$TaskName' task found; unregistering first."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" -Url `"$Url`" -Iterations 3 -SleepSeconds 20"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $WorkspaceRoot
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration (New-TimeSpan -Days 9999))
)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 75) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Read-only ETA dashboard diagnostics cache warmer. Order action: never submits, cancels, flattens, or promotes." `
    -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
    -Force | Out-Null

if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
}

Write-Host "OK: Registered '$TaskName' as SYSTEM, AtStartup+AtLogOn+every-minute."
Write-Host "    Runner:   $Runner"
Write-Host "    URL:      $Url"
Write-Host "    Logs:     $LogDir"
Write-Host "    Start now with:  Start-ScheduledTask -TaskName '$TaskName'"
