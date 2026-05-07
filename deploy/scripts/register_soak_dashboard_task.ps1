# ETA // register_soak_dashboard_task.ps1
# ==========================================
# Register the Paper Soak Dashboard as a durable Windows Scheduled Task
# so it survives VPS reboots. Serves:
#   - HTML dashboard at http://127.0.0.1:8424/
#   - JSON endpoint at http://127.0.0.1:8424/api/soak/status
#
# Runs alongside the existing 8421 proxy and 8422 FM status server.
# Pattern matches register_dashboard_api_task.ps1 and register_fleet_tasks.ps1.
#
# Usage (run elevated):
#   powershell.exe -ExecutionPolicy Bypass -File `
#     C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\register_soak_dashboard_task.ps1
#
# Or with -Start to launch immediately:
#   powershell.exe -ExecutionPolicy Bypass -File `
#     C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\register_soak_dashboard_task.ps1 -Start

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Start,
    [string]$InstallRoot = "C:\EvolutionaryTradingAlgo",
    [string]$RunAsUser = ""
)

$ErrorActionPreference = "Stop"

$TaskName = "ETA-SoakDashboard"
$EtaEngineDir = Join-Path $InstallRoot "eta_engine"
$WorkspaceRoot = $InstallRoot
$StateDir = Join-Path $WorkspaceRoot "var\eta_engine\state"
$LogDir = Join-Path $WorkspaceRoot "logs\eta_engine"
$StdoutLog = Join-Path $LogDir "soak_dashboard.stdout.log"
$StderrLog = Join-Path $LogDir "soak_dashboard.stderr.log"
$ApiScript = Join-Path $EtaEngineDir "deploy\status_page\soak_status_api.py"
$VenvPython = Join-Path $EtaEngineDir ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python.exe" }

function Assert-CanonicalPath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing non-canonical path: $Path (resolved: $resolved)"
    }
}

Assert-CanonicalPath -Path $EtaEngineDir
Assert-CanonicalPath -Path $WorkspaceRoot
Assert-CanonicalPath -Path $StateDir
Assert-CanonicalPath -Path $LogDir
Assert-CanonicalPath -Path $StdoutLog
Assert-CanonicalPath -Path $StderrLog
Assert-CanonicalPath -Path $ApiScript

if (-not (Test-Path -LiteralPath $ApiScript)) {
    throw "Missing soak status API script: $ApiScript"
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-Warning "Python executable not found at '$PythonExe'; using system python.exe"
    $PythonExe = "python.exe"
}

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Install root : $InstallRoot"
    Write-Host "  ETA engine   : $EtaEngineDir"
    Write-Host "  Python       : $PythonExe"
    Write-Host "  API script   : $ApiScript"
    Write-Host "  State dir    : $StateDir"
    Write-Host "  Stdout log   : $StdoutLog"
    Write-Host "  Stderr log   : $StderrLog"
    Write-Host "  Port         : 8424"
    Write-Host "  Triggers     : AtStartup + AtLogOn"
    Write-Host "  Restart      : 999 attempts, 1-minute delay"
    exit 0
}

# Auto-detect principal from existing ETA-* tasks
if (-not $RunAsUser) {
    $existingTask = Get-ScheduledTask -TaskName "ETA-Dashboard" -ErrorAction SilentlyContinue
    if ($existingTask) {
        $RunAsUser = $existingTask.Principal.UserId
        Write-Host "Auto-detected RunAsUser from ETA-Dashboard: $RunAsUser"
    }
    else {
        $RunAsUser = "NT AUTHORITY\SYSTEM"
        Write-Host "No ETA-* task found; defaulting RunAsUser to SYSTEM"
    }
}

# Stop + unregister existing task
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Existing '$TaskName' task found; unregistering first."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

# Ensure dirs exist
New-Item -ItemType Directory -Force -Path $StateDir, $LogDir | Out-Null

# Kill any existing instances on port 8424
Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { ([string]$_.CommandLine) -match "soak_status_api" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$Arguments = "-u `"$ApiScript`""

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $Arguments -WorkingDirectory $EtaEngineDir
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn)
)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

if ($RunAsUser -eq "NT AUTHORITY\SYSTEM") {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
}
else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId $RunAsUser -LogonType S4U -RunLevel Highest
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "ETA Paper Soak Dashboard — HTML + JSON API on 127.0.0.1:8424. Serves fleet soak status, PnL, Sharpe ratios, and diamond designations." `
    -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
    -Force | Out-Null

if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "==> Task started."
}

Write-Host "OK: Registered '$TaskName'"
Write-Host "    API script : $ApiScript"
Write-Host "    Python     : $PythonExe"
Write-Host "    Port       : 8424"
Write-Host "    Endpoints  : http://127.0.0.1:8424/  (HTML dashboard)"
Write-Host "                 http://127.0.0.1:8424/api/soak/status  (JSON)"
Write-Host "                 http://127.0.0.1:8424/health  (health check)"
Write-Host "    State dir  : $StateDir"
Write-Host "    Logs       : $StdoutLog"
Write-Host "                 $StderrLog"
Write-Host "    Principal  : $RunAsUser"
Write-Host "    Triggers   : AtStartup + AtLogOn, restart on fail"
Write-Host ""
Write-Host "    Start now  : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "    Stop       : Stop-ScheduledTask -TaskName '$TaskName'"
