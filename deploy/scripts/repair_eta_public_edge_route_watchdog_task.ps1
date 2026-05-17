[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$CurrentUser,
    [switch]$Start,
    [int]$RecoveryIntervalMinutes = 5
)

$ErrorActionPreference = "Stop"

$taskName = "ETA-Public-Edge-Route-Watchdog"
$workspaceRoot = "C:\EvolutionaryTradingAlgo"
$etaEngineRoot = Join-Path $workspaceRoot "eta_engine"
$scriptModule = "eta_engine.scripts.public_edge_route_watchdog"
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

$scriptPath = Join-Path $etaEngineRoot "scripts\public_edge_route_watchdog.py"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Canonical public_edge_route_watchdog.py not found at $scriptPath"
}
if ($RecoveryIntervalMinutes -lt 1) {
    throw "RecoveryIntervalMinutes must be at least 1, got: $RecoveryIntervalMinutes"
}

$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument "-m $scriptModule --once --json" `
    -WorkingDirectory $workspaceRoot

$recoveryTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $RecoveryIntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew

$description = "ETA: canonical public edge route watchdog. Probes 127.0.0.1:8081 against 127.0.0.1:8421 and repairs FirmCommandCenterEdge route drift."

if ($DryRun) {
    [pscustomobject]@{
        task_name = $taskName
        execute = $pythonPath
        arguments = $action.Arguments
        working_directory = $workspaceRoot
        trigger = "startup_logon_every_${RecoveryIntervalMinutes}m"
        module = $scriptModule
        principal = if ($CurrentUser) { "$env:USERDOMAIN\$env:USERNAME (Interactive, Limited)" } else { "NT AUTHORITY\SYSTEM (ServiceAccount, Highest)" }
        description = $description
    } | Format-List
    return
}

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $CurrentUser -and -not $isAdmin) {
    throw "Administrator rights are required to register $taskName as SYSTEM. Use repair_eta_public_edge_route_watchdog_admin.cmd or rerun from an elevated shell."
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
    $triggers = @($logonTrigger, $recoveryTrigger)
    $principalLabel = "$env:USERDOMAIN\$env:USERNAME, Interactive/Limited"
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "NT AUTHORITY\SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest
    $triggers = @($startupTrigger, $logonTrigger, $recoveryTrigger)
    $principalLabel = "SYSTEM, Highest"
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $triggers `
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
