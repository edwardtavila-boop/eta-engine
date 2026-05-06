# EVOLUTIONARY TRADING ALGO // register_dashboard_api_task.ps1
# Registers the canonical dashboard API as a durable Windows Scheduled Task.
# The task action intentionally points at run_dashboard_api_task.cmd so we do
# not depend on fragile long inline Scheduled Task command strings.

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$TaskName = "ETA-Dashboard-API"
$WorkingDir = "C:\EvolutionaryTradingAlgo\eta_engine"
$WorkspaceRoot = "C:\EvolutionaryTradingAlgo"
$StateDir = Join-Path $WorkspaceRoot "var\eta_engine\state"
$LogDir = Join-Path $WorkspaceRoot "logs\eta_engine"
$StdoutLog = Join-Path $LogDir "dashboard_api.stdout.log"
$StderrLog = Join-Path $LogDir "dashboard_api.stderr.log"
$Runner = Join-Path $WorkingDir "deploy\scripts\run_dashboard_api_task.cmd"
$VenvPython = Join-Path $WorkingDir ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { "python.exe" }

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
Assert-CanonicalEtaPath -Path $StdoutLog
Assert-CanonicalEtaPath -Path $StderrLog
Assert-CanonicalEtaPath -Path $Runner

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Missing dashboard API task runner: $Runner"
}

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Working dir : $WorkingDir"
    Write-Host "  Python      : $PythonExe"
    Write-Host "  Runner      : $Runner"
    Write-Host "  State dir   : $StateDir"
    Write-Host "  Stdout log  : $StdoutLog"
    Write-Host "  Stderr log  : $StderrLog"
    Write-Host "  Principal   : NT AUTHORITY\SYSTEM (Highest)"
    Write-Host "  Triggers    : AtStartup + AtLogOn"
    Write-Host "  Restart     : 999 attempts, 1-minute delay"
    exit 0
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Existing '$TaskName' task found; unregistering first."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force -Path $StateDir, $LogDir | Out-Null

Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { ([string]$_.CommandLine) -match "deploy\.scripts\.dashboard_api:app|deploy.scripts.dashboard_api:app" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Get-CimInstance Win32_Process -Filter "name='cmd.exe'" -ErrorAction SilentlyContinue |
    Where-Object { ([string]$_.CommandLine) -match "run_dashboard_api_task\.cmd|deploy\.scripts\.dashboard_api:app|deploy.scripts.dashboard_api:app" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$action = New-ScheduledTaskAction -Execute $Runner -WorkingDirectory $WorkingDir
$triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Canonical ETA dashboard API on 127.0.0.1:8000 with state/logs under C:\EvolutionaryTradingAlgo." `
    -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
    -Force | Out-Null

if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
}

Write-Host "OK: Registered '$TaskName' as SYSTEM, AtStartup+AtLogOn, restart-on-fail."
Write-Host "    Runner: $Runner"
Write-Host "    Logs:   $StdoutLog"
Write-Host "            $StderrLog"
Write-Host "    Start now with:  Start-ScheduledTask -TaskName '$TaskName'"
