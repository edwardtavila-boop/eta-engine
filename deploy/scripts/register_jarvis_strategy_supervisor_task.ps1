# EVOLUTIONARY TRADING ALGO // register_jarvis_strategy_supervisor_task.ps1
# Registers the paper-live JARVIS strategy supervisor as a durable Windows
# Scheduled Task with explicit env vars and canonical stdout/stderr logs.

[CmdletBinding()]
param([switch]$DryRun)

$ErrorActionPreference = "Stop"

$TaskName = "ETA-Jarvis-Strategy-Supervisor"
$WorkingDir = "C:\EvolutionaryTradingAlgo\eta_engine"
$WorkspaceRoot = "C:\EvolutionaryTradingAlgo"
$LogDir = Join-Path $WorkspaceRoot "logs\eta_engine"
$StdoutLog = Join-Path $LogDir "jarvis_strategy_supervisor.stdout.log"
$StderrLog = Join-Path $LogDir "jarvis_strategy_supervisor.stderr.log"
$Runner = Join-Path $WorkingDir "deploy\scripts\run_jarvis_strategy_supervisor_task.cmd"
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
Assert-CanonicalEtaPath -Path $LogDir
Assert-CanonicalEtaPath -Path $StdoutLog
Assert-CanonicalEtaPath -Path $StderrLog
Assert-CanonicalEtaPath -Path $Runner

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Missing supervisor task runner: $Runner"
}

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Working dir : $WorkingDir"
    Write-Host "  Python      : $PythonExe"
    Write-Host "  Runner      : $Runner"
    Write-Host "  Stdout log  : $StdoutLog"
    Write-Host "  Stderr log  : $StderrLog"
    Write-Host "  Cmd line    : $Runner"
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

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$action = New-ScheduledTaskAction -Execute $Runner -WorkingDirectory $WorkingDir
$triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$description = "JARVIS strategy supervisor: paper-live composite feed with canonical heartbeat and logs."
$systemPrincipal = New-ScheduledTaskPrincipal `
    -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description $description `
        -Action $action -Trigger $triggers -Settings $settings -Principal $systemPrincipal `
        -Force | Out-Null
    $principalLabel = "SYSTEM, AtStartup+AtLogOn"
} catch {
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    Write-Warning "SYSTEM registration unavailable: $($_.Exception.Message)"
    $userTriggers = @((New-ScheduledTaskTrigger -AtLogOn -User $currentUser))
    $userPrincipal = New-ScheduledTaskPrincipal `
        -UserId $currentUser -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description $description `
        -Action $action -Trigger $userTriggers -Settings $settings -Principal $userPrincipal `
        -Force | Out-Null
    $principalLabel = "current_user:$currentUser, AtLogOn"
}

Write-Host "OK: Registered '$TaskName' as $principalLabel, restart-on-fail."
Write-Host "    Logs: $StdoutLog"
Write-Host "          $StderrLog"
Write-Host "    Start now with:  Start-ScheduledTask -TaskName '$TaskName'"
