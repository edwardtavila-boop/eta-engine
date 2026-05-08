# EVOLUTIONARY TRADING ALGO // register_dashboard_proxy_watchdog_task.ps1
# Registers a long-running watchdog for the public dashboard proxy bridge.
#
# The bridge task can exit cleanly while Cloudflare still depends on port 8421.
# This watchdog probes 127.0.0.1:8421 and starts ETA-Proxy-8421 when the
# premium dashboard marker is not present.

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "ETA-Dashboard-Proxy-Watchdog",
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

function Resolve-PythonRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$EngineDir,
        [string]$ExplicitPython
    )

    if ($ExplicitPython) {
        if (-not (Test-Path -LiteralPath $ExplicitPython)) {
            throw "Missing explicit Python runtime: $ExplicitPython"
        }
        return (Resolve-Path -LiteralPath $ExplicitPython).Path
    }

    $venvPython = Join-Path $EngineDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return (Resolve-Path -LiteralPath $venvPython).Path
    }

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source) {
        return $pythonCommand.Source
    }

    throw "Missing Python runtime: no .venv python under $EngineDir and no python.exe on PATH"
}

$RootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd("\")
if ($RootFull -ne "C:\EvolutionaryTradingAlgo") {
    throw "Expected canonical ETA root C:\EvolutionaryTradingAlgo, got: $RootFull"
}

$EngineDir = Join-Path $RootFull "eta_engine"
$StateDir = Join-Path $RootFull "var\eta_engine\state"
$LogDir = Join-Path $RootFull "logs\eta_engine"
$WatchdogScript = Join-Path $EngineDir "scripts\dashboard_proxy_watchdog.py"
$Python = Resolve-PythonRuntime -EngineDir $EngineDir -ExplicitPython $PythonExe

Assert-CanonicalEtaPath -Path $EngineDir
Assert-CanonicalEtaPath -Path $StateDir
Assert-CanonicalEtaPath -Path $LogDir
Assert-CanonicalEtaPath -Path $WatchdogScript

if (-not (Test-Path -LiteralPath $WatchdogScript)) {
    throw "Missing dashboard proxy watchdog: $WatchdogScript"
}
if ($IntervalSeconds -lt 5) {
    throw "IntervalSeconds must be at least 5, got: $IntervalSeconds"
}

& $Python -m py_compile $WatchdogScript
New-Item -ItemType Directory -Force -Path $StateDir, $LogDir | Out-Null

if ($RestartExistingProcess) {
    Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { ([string]$_.CommandLine) -match "dashboard_proxy_watchdog" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$arguments = "-m eta_engine.scripts.dashboard_proxy_watchdog --interval-s $IntervalSeconds"
$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $RootFull
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

if ($PSCmdlet.ShouldProcess($TaskName, "Register dashboard proxy watchdog task")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $principal `
        -Description "ETA dashboard proxy watchdog. Probes 127.0.0.1:8421 and restarts ETA-Proxy-8421 when stale." `
        -Force | Out-Null

    if ($Start) {
        Start-ScheduledTask -TaskName $TaskName
    }

    Get-ScheduledTask -TaskName $TaskName |
        Select-Object TaskName,State,@{Name="UserId";Expression={$_.Principal.UserId}},@{Name="PythonExe";Expression={$Python}},@{Name="IntervalSeconds";Expression={$IntervalSeconds}}
}
