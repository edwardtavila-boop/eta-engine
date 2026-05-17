[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$CurrentUser,
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$taskName = "ETA-WeeklySharpe"
$workspaceRoot = "C:\EvolutionaryTradingAlgo"
$etaEngineRoot = Join-Path $workspaceRoot "eta_engine"
$scriptModule = "eta_engine.scripts.weekly_sharpe_check"
$venvPython = Join-Path $etaEngineRoot ".venv\Scripts\python.exe"
$machinePython = "C:\Python314\python.exe"

function Assert-CanonicalEtaPath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
    $canonicalRoot = "C:\EvolutionaryTradingAlgo"
    if (
        $resolved -ne $canonicalRoot -and
        -not $resolved.StartsWith("$canonicalRoot\", [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Refusing non-canonical ETA path: $Path"
    }
}

Assert-CanonicalEtaPath -Path $workspaceRoot
Assert-CanonicalEtaPath -Path $etaEngineRoot

$pythonPath = [Environment]::GetEnvironmentVariable("ETA_PYTHON_EXE", "Machine")
if (-not $pythonPath) {
    $pythonPath = [Environment]::GetEnvironmentVariable("ETA_PYTHON_EXE", "User")
}
if (-not $pythonPath -and (Test-Path -LiteralPath $venvPython)) {
    $pythonPath = $venvPython
}
if (-not $pythonPath -and (Test-Path -LiteralPath $machinePython)) {
    $pythonPath = $machinePython
}
if (-not $pythonPath) {
    throw "No canonical Python executable found for $taskName. Expected ETA_PYTHON_EXE, $venvPython, or $machinePython"
}
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Python executable not found at $pythonPath"
}

$scriptPath = Join-Path $etaEngineRoot "scripts\weekly_sharpe_check.py"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Canonical weekly_sharpe_check.py not found at $scriptPath"
}

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "11:00 PM"

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument "-m $scriptModule" `
    -WorkingDirectory $workspaceRoot

$description = "ETA: canonical weekly Sharpe gate from C:\EvolutionaryTradingAlgo\eta_engine\scripts\weekly_sharpe_check.py (Sunday 11:00 PM)"

if ($DryRun) {
    [pscustomobject]@{
        task_name = $taskName
        execute = $pythonPath
        arguments = $action.Arguments
        working_directory = $workspaceRoot
        trigger = "weekly_sunday_11pm"
        module = $scriptModule
        principal = if ($CurrentUser) { "$env:USERDOMAIN\$env:USERNAME (Interactive, Limited)" } else { "NT AUTHORITY\SYSTEM (ServiceAccount, Highest)" }
        description = $description
    } | Format-List
    return
}

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $CurrentUser -and -not $isAdmin) {
    throw "Administrator rights are required to register $taskName as SYSTEM. Use repair_eta_weekly_sharpe_admin.cmd or rerun from an elevated shell."
}

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
}

if ($CurrentUser) {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited
    $principalLabel = "$env:USERDOMAIN\$env:USERNAME, Interactive/Limited"
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "NT AUTHORITY\SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest
    $principalLabel = "SYSTEM, Highest"
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description $description `
    -Force | Out-Null

if ($Start) {
    Start-ScheduledTask -TaskName $taskName
}

Write-Host "OK: Registered '$taskName' as $principalLabel."
Write-Host "    Python : $pythonPath"
Write-Host "    Module : $scriptModule"
Write-Host "    CWD    : $workspaceRoot"
Write-Host "    Start manually with: Start-ScheduledTask -TaskName '$taskName'"
