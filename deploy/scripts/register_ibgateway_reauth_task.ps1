# EVOLUTIONARY TRADING ALGO // register_ibgateway_reauth_task.ps1
# Register a safe IB Gateway recovery controller. This task does not launch
# ibgateway.exe directly; it starts the existing Gateway scheduled tasks so the
# configured Gateway profile and Windows user ownership stay intact.

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Start,
    [switch]$CurrentUser,
    [ValidateRange(30, 300)]
    [int]$IntervalSeconds = 60
)

$ErrorActionPreference = "Stop"

$TaskName = "ETA-IBGateway-Reauth"
$WorkingDir = "C:\EvolutionaryTradingAlgo\eta_engine"
$VenvPython = Join-Path $WorkingDir ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { "python.exe" }
$StateDir = "C:\EvolutionaryTradingAlgo\var\eta_engine\state"

if (-not (Test-Path -LiteralPath $WorkingDir)) {
    throw "Missing canonical ETA engine directory: $WorkingDir"
}

$cmdLine = "/c cd /d ""$WorkingDir"" && ""$PythonExe"" -m eta_engine.scripts.ibgateway_reauth_controller --execute"

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Working dir : $WorkingDir"
    Write-Host "  Python      : $PythonExe"
    Write-Host "  State dir   : $StateDir"
    Write-Host "  Cmd line    : cmd.exe $cmdLine"
    if ($CurrentUser) {
        Write-Host "  Principal   : current user (Interactive/Limited)"
        Write-Host "  Triggers    : AtLogOn + every $IntervalSeconds seconds"
    } else {
        Write-Host "  Principal   : NT AUTHORITY\SYSTEM (Highest), with current-user fallback"
        Write-Host "  Triggers    : AtStartup + every $IntervalSeconds seconds"
    }
    Write-Host "  Start now   : $($Start.IsPresent)"
    exit 0
}

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Existing '$TaskName' task found; unregistering first."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdLine -WorkingDirectory $WorkingDir
$heartbeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Seconds $IntervalSeconds) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$triggers = @((New-ScheduledTaskTrigger -AtStartup), $heartbeat)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
$description = "Canonical IB Gateway recovery controller: reads watchdog health and starts the canonical Gateway tasks when safe."
$registeredPrincipal = "SYSTEM"
if ($CurrentUser) {
    $currentUserName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal `
        -UserId $currentUserName -LogonType Interactive -RunLevel Limited
    $triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $currentUserName), $heartbeat)
    $description = "$description Current-user fallback requested by operator."
    $registeredPrincipal = "current_user:$currentUserName"
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
}

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description $description `
        -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
        -Force | Out-Null
} catch {
    if ($CurrentUser) {
        throw
    }
    $currentUserName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $fallbackPrincipal = New-ScheduledTaskPrincipal `
        -UserId $currentUserName -LogonType Interactive -RunLevel Limited
    $fallbackTriggers = @((New-ScheduledTaskTrigger -AtLogOn -User $currentUserName), $heartbeat)
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description "$description Current-user fallback because SYSTEM registration was unavailable." `
        -Action $action -Trigger $fallbackTriggers -Settings $settings -Principal $fallbackPrincipal `
        -Force | Out-Null
    $registeredPrincipal = "current_user:$currentUserName"
}

Write-Host "OK: Registered '$TaskName' as $registeredPrincipal, startup/logon plus every $IntervalSeconds seconds."
if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "    Started '$TaskName' immediately."
} else {
    Write-Host "    Start now with:  Start-ScheduledTask -TaskName '$TaskName'"
}
