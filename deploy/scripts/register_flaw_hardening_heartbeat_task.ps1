# EVOLUTIONARY TRADING ALGO // register_flaw_hardening_heartbeat_task.ps1
# Registers a low-overhead read-only flaw hardening snapshot heartbeat.

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$CurrentUser,
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$TaskName = "ETA-FlawHardeningHeartbeat"
$WorkspaceRoot = "C:\EvolutionaryTradingAlgo"
$WorkingDir = Join-Path $WorkspaceRoot "eta_engine"
$StateDir = Join-Path $WorkspaceRoot "var\eta_engine\state"
$LogDir = Join-Path $WorkspaceRoot "logs\eta_engine"
$Runner = Join-Path $WorkingDir "deploy\scripts\run_flaw_hardening_heartbeat_task.cmd"
$Artifact = Join-Path $StateDir "flaw_hardening_snapshot.json"
$PreviousArtifact = Join-Path $StateDir "flaw_hardening_snapshot.previous.json"

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
Assert-CanonicalEtaPath -Path $PreviousArtifact

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Missing flaw hardening heartbeat runner: $Runner"
}

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Working dir : $WorkingDir"
    Write-Host "  Runner      : $Runner"
    Write-Host "  Artifact    : $Artifact"
    Write-Host "  Previous    : $PreviousArtifact"
    Write-Host "  State dir   : $StateDir"
    Write-Host "  Log dir     : $LogDir"
    if ($CurrentUser) {
        Write-Host "  Principal   : $env:USERDOMAIN\$env:USERNAME (Interactive, Limited)"
    } else {
        Write-Host "  Principal   : NT AUTHORITY\SYSTEM (Highest)"
    }
    Write-Host "  Triggers    : AtStartup + AtLogOn + every 10 minutes"
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
        -RepetitionInterval (New-TimeSpan -Minutes 10) `
        -RepetitionDuration (New-TimeSpan -Days 9999))
)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
    -MultipleInstances IgnoreNew
if ($CurrentUser) {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $principalLabel = "$env:USERDOMAIN\$env:USERNAME, Interactive/Limited"
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $principalLabel = "SYSTEM, Highest"
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Refreshes the read-only ETA flaw hardening snapshot under C:\EvolutionaryTradingAlgo." `
    -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
    -Force | Out-Null

if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
}

Write-Host "OK: Registered '$TaskName' as $principalLabel, AtStartup+AtLogOn+every-10m."
Write-Host "    Runner:   $Runner"
Write-Host "    Artifact: $Artifact"
Write-Host "    Logs:     $LogDir"
Write-Host "    Start now with:  Start-ScheduledTask -TaskName '$TaskName'"
