# EVOLUTIONARY TRADING ALGO // register_symbol_intelligence_collector_task.ps1
# Registers the canonical symbol-intelligence data collector for the 24/7 VPS.

[CmdletBinding()]
param(
    [int]$IntervalMinutes = 5,
    [switch]$DryRun,
    [switch]$Start,
    [switch]$RetireLegacyTier2Task = $true
)

$ErrorActionPreference = "Stop"

$TaskName = "ETA-SymbolIntelCollector"
$WorkspaceRoot = "C:\EvolutionaryTradingAlgo"
$WorkingDir = Join-Path $WorkspaceRoot "eta_engine"
$StateDir = Join-Path $WorkspaceRoot "var\eta_engine\state"
$LogDir = Join-Path $WorkspaceRoot "logs\eta_engine"
$Runner = Join-Path $WorkingDir "deploy\scripts\run_symbol_intelligence_collector.cmd"
$Artifact = Join-Path $StateDir "symbol_intelligence_collector_latest.json"
$LegacyTier2Task = "EtaTier2SnapshotSync"

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
    throw "IntervalMinutes must stay between 1 and 30 for 24/7 collector freshness."
}

$WorkspaceRoot = Assert-CanonicalEtaPath -Path $WorkspaceRoot
$WorkingDir = Assert-CanonicalEtaPath -Path $WorkingDir
$StateDir = Assert-CanonicalEtaPath -Path $StateDir
$LogDir = Assert-CanonicalEtaPath -Path $LogDir
$Runner = Assert-CanonicalEtaPath -Path $Runner
$Artifact = Assert-CanonicalEtaPath -Path $Artifact

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Missing symbol-intelligence collector runner: $Runner"
}

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Working dir : $WorkspaceRoot"
    Write-Host "  Runner      : $Runner"
    Write-Host "  Artifact    : $Artifact"
    Write-Host "  Principal   : NT AUTHORITY\SYSTEM (Highest), with current-user fallback"
    Write-Host "  Triggers    : AtStartup + AtLogOn + every $IntervalMinutes minutes"
    Write-Host "  Retire old  : $LegacyTier2Task = $($RetireLegacyTier2Task.IsPresent)"
    exit 0
}

New-Item -ItemType Directory -Force -Path $StateDir, $LogDir | Out-Null

if ($RetireLegacyTier2Task) {
    $legacy = Get-ScheduledTask -TaskName $LegacyTier2Task -ErrorAction SilentlyContinue
    if ($legacy) {
        Write-Host "==> Retiring stale legacy data task '$LegacyTier2Task'."
        Stop-ScheduledTask -TaskName $LegacyTier2Task -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $LegacyTier2Task -Confirm:$false -ErrorAction SilentlyContinue
    }
}

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
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
$principal = New-ScheduledTaskPrincipal `
    -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$description = "ETA symbol-intelligence collector: refreshes bars/events/decisions/outcomes/quality under C:\EvolutionaryTradingAlgo."
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
