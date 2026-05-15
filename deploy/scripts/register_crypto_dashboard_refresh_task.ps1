# EVOLUTIONARY TRADING ALGO // register_crypto_dashboard_refresh_task.ps1
# Registers the lightweight BTC/ETH/SOL dashboard bar refresh heartbeat.

[CmdletBinding()]
param(
    [int]$IntervalMinutes = 5,
    [switch]$DryRun,
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$TaskName = "ETA-Crypto-Dashboard-Refresh"
$WorkspaceRoot = "C:\EvolutionaryTradingAlgo"
$WorkingDir = Join-Path $WorkspaceRoot "eta_engine"
$StateDir = Join-Path $WorkspaceRoot "var\eta_engine\state"
$LogDir = Join-Path $WorkspaceRoot "logs\eta_engine"
$Runner = Join-Path $WorkingDir "deploy\scripts\run_crypto_dashboard_refresh_task.cmd"
$Artifact = Join-Path $StateDir "crypto_dashboard_refresh_latest.json"

function Assert-CanonicalEtaPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
    if (
        $resolved -ne "C:\EvolutionaryTradingAlgo" -and
        -not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Refusing non-canonical ETA path: $Path"
    }
    return $resolved
}

if ($IntervalMinutes -lt 1 -or $IntervalMinutes -gt 30) {
    throw "IntervalMinutes must stay between 1 and 30 for dashboard freshness."
}

$WorkspaceRoot = Assert-CanonicalEtaPath -Path $WorkspaceRoot
$WorkingDir = Assert-CanonicalEtaPath -Path $WorkingDir
$StateDir = Assert-CanonicalEtaPath -Path $StateDir
$LogDir = Assert-CanonicalEtaPath -Path $LogDir
$Runner = Assert-CanonicalEtaPath -Path $Runner
$Artifact = Assert-CanonicalEtaPath -Path $Artifact

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Missing crypto dashboard refresh runner: $Runner"
}

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Working dir : $WorkspaceRoot"
    Write-Host "  Runner      : $Runner"
    Write-Host "  Artifact    : $Artifact"
    Write-Host "  Principal   : NT AUTHORITY\SYSTEM (Highest), with current-user fallback"
    Write-Host "  Triggers    : AtStartup + AtLogOn + every $IntervalMinutes minutes"
    Write-Host "  Data path   : refreshes BTC/ETH/SOL dashboard bar freshness via Coinbase public candles"
    Write-Host "  Order action: never submits, cancels, flattens, or promotes"
    exit 0
}

New-Item -ItemType Directory -Force -Path $StateDir, $LogDir | Out-Null

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Existing '$TaskName' task found; unregistering first."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction -Execute $Runner -WorkingDirectory $WorkspaceRoot
$heartbeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn), $heartbeat)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal `
    -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$description = "ETA crypto dashboard refresh: keeps BTC/ETH/SOL 5-minute dashboard bars fresh under C:\EvolutionaryTradingAlgo using Coinbase public candles."
$registeredPrincipal = "SYSTEM"

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description $description `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null
} catch {
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $fallbackPrincipal = New-ScheduledTaskPrincipal `
        -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $fallbackTriggers = @((New-ScheduledTaskTrigger -AtLogOn -User $currentUser), $heartbeat)
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description "$description Current-user fallback because SYSTEM registration was unavailable." `
        -Action $action `
        -Trigger $fallbackTriggers `
        -Settings $settings `
        -Principal $fallbackPrincipal `
        -Force | Out-Null
    $registeredPrincipal = "current_user:$currentUser"
}

if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
}

Write-Host "OK: Registered '$TaskName' as $registeredPrincipal, startup/logon plus every $IntervalMinutes minutes."
Write-Host "    Runner:   $Runner"
Write-Host "    Artifact: $Artifact"
Write-Host "    Logs:     $LogDir"
