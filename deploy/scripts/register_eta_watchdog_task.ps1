# EVOLUTIONARY TRADING ALGO // register_eta_watchdog_task.ps1
# Registers the long-running ETA watchdog as a durable boot/logon task.
#
# The watchdog process already loops internally via --interval-s. Do not add a
# repeating Task Scheduler trigger, or Windows records duplicate-launch refusals
# while the healthy long-running instance is still active.

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "ETA-Watchdog",
    [string]$Root = "C:\EvolutionaryTradingAlgo",
    [string]$PythonExe = "",
    [int]$IntervalSeconds = 60,
    [switch]$Start,
    [switch]$RestartExistingProcess
)

$ErrorActionPreference = "Stop"

function Assert-CanonicalEtaPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing non-canonical ETA path: $Path"
    }
}

$RootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd("\")
if ($RootFull -ne "C:\EvolutionaryTradingAlgo") {
    throw "Expected canonical ETA root C:\EvolutionaryTradingAlgo, got: $RootFull"
}

$EngineDir = Join-Path $RootFull "eta_engine"
$StateDir = Join-Path $RootFull "var\eta_engine\state"
$LogDir = Join-Path $RootFull "logs\eta_engine"

if (-not $PythonExe) {
    $VenvPython = Join-Path $EngineDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython) {
        $PythonExe = $VenvPython
    } else {
        $PythonCmd = Get-Command python -ErrorAction SilentlyContinue
        if ($PythonCmd) {
            $PythonExe = $PythonCmd.Source
        }
    }
}

Assert-CanonicalEtaPath -Path $EngineDir
Assert-CanonicalEtaPath -Path $StateDir
Assert-CanonicalEtaPath -Path $LogDir

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Missing Python executable: $PythonExe"
}
if (-not (Test-Path -LiteralPath $EngineDir)) {
    throw "Missing ETA engine directory: $EngineDir"
}
if ($IntervalSeconds -lt 5) {
    throw "IntervalSeconds must be at least 5, got: $IntervalSeconds"
}

New-Item -ItemType Directory -Force -Path $StateDir, $LogDir | Out-Null

if ($RestartExistingProcess) {
    Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { ([string]$_.CommandLine) -match "eta_engine\.scripts\.eta_watchdog|eta_engine.scripts.eta_watchdog" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m eta_engine.scripts.eta_watchdog --interval-s $IntervalSeconds" `
    -WorkingDirectory $RootFull
$triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal `
    -UserId "NT AUTHORITY\SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

if ($PSCmdlet.ShouldProcess($TaskName, "Register ETA watchdog task")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $principal `
        -Description "ETA long-running runtime watchdog. Uses boot/logon triggers only; the process loops internally." `
        -Force | Out-Null

    if ($Start) {
        Start-ScheduledTask -TaskName $TaskName
    }

    Get-ScheduledTask -TaskName $TaskName |
        Select-Object TaskName,State,@{Name="UserId";Expression={$_.Principal.UserId}},@{Name="PythonExe";Expression={$PythonExe}},@{Name="IntervalSeconds";Expression={$IntervalSeconds}}
}
