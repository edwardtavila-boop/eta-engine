# EVOLUTIONARY TRADING ALGO // register_vps_ops_hardening_audit_task.ps1
# Registers a low-overhead read-only VPS operations hardening audit.

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$TaskName = "ETA-VpsOpsHardeningAudit"
$WorkspaceRoot = "C:\EvolutionaryTradingAlgo"
$WorkingDir = Join-Path $WorkspaceRoot "eta_engine"
$StateDir = Join-Path $WorkspaceRoot "var\eta_engine\state"
$LogDir = Join-Path $WorkspaceRoot "logs\eta_engine"
$Runner = Join-Path $WorkingDir "deploy\scripts\run_vps_ops_hardening_audit.cmd"
$Artifact = Join-Path $StateDir "vps_ops_hardening_latest.json"

function Assert-CanonicalEtaPath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing non-canonical ETA path: $Path"
    }
}

Assert-CanonicalEtaPath -Path $WorkingDir
Assert-CanonicalEtaPath -Path $StateDir
Assert-CanonicalEtaPath -Path $LogDir
Assert-CanonicalEtaPath -Path $Runner
Assert-CanonicalEtaPath -Path $Artifact

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Missing VPS ops hardening runner: $Runner"
}

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Working dir : $WorkingDir"
    Write-Host "  Runner      : $Runner"
    Write-Host "  Artifact    : $Artifact"
    Write-Host "  State dir   : $StateDir"
    Write-Host "  Log dir     : $LogDir"
    Write-Host "  Principal   : NT AUTHORITY\SYSTEM (Highest)"
    Write-Host "  Triggers    : AtStartup + AtLogOn + every 5 minutes"
    Write-Host "  Order action: never submits, cancels, flattens, or promotes"
    exit 0
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Existing '$TaskName' task found; unregistering first."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force -Path $StateDir, $LogDir | Out-Null

$action = New-ScheduledTaskAction -Execute $Runner -WorkingDirectory $WorkspaceRoot
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Days 9999))
)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Refreshes read-only ETA VPS ops hardening status under C:\EvolutionaryTradingAlgo." `
    -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
    -Force | Out-Null

if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
}

Write-Host "OK: Registered '$TaskName' as SYSTEM, AtStartup+AtLogOn+every-5m."
Write-Host "    Runner:   $Runner"
Write-Host "    Artifact: $Artifact"
Write-Host "    Logs:     $LogDir"
Write-Host "    Start now with:  Start-ScheduledTask -TaskName '$TaskName'"
