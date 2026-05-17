# EVOLUTIONARY TRADING ALGO // register_public_edge_route_watchdog_task.ps1
# Registers a bounded watchdog tick for the public dashboard edge route.
#
# The public edge on 127.0.0.1:8081 must mirror the canonical 127.0.0.1:8421
# route. This watchdog repairs Caddy route drift back to legacy 8420 and
# restarts only FirmCommandCenterEdge when the public surface no longer matches
# the canonical operator bridge.

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "ETA-Public-Edge-Route-Watchdog",
    [string]$Root = "C:\EvolutionaryTradingAlgo",
    [string]$PythonExe = "",
    [int]$RecoveryIntervalMinutes = 5,
    [switch]$CurrentUser,
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
$WatchdogScript = Join-Path $EngineDir "scripts\public_edge_route_watchdog.py"
$Python = Resolve-PythonRuntime -EngineDir $EngineDir -ExplicitPython $PythonExe

Assert-CanonicalEtaPath -Path $EngineDir
Assert-CanonicalEtaPath -Path $StateDir
Assert-CanonicalEtaPath -Path $LogDir
Assert-CanonicalEtaPath -Path $WatchdogScript

if (-not (Test-Path -LiteralPath $WatchdogScript)) {
    throw "Missing public edge route watchdog: $WatchdogScript"
}
if ($RecoveryIntervalMinutes -lt 1) {
    throw "RecoveryIntervalMinutes must be at least 1, got: $RecoveryIntervalMinutes"
}

& $Python -m py_compile $WatchdogScript
New-Item -ItemType Directory -Force -Path $StateDir, $LogDir | Out-Null

if ($RestartExistingProcess) {
    Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { ([string]$_.CommandLine) -match "public_edge_route_watchdog" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$arguments = "-m eta_engine.scripts.public_edge_route_watchdog --once --json"
$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $RootFull
$recoveryTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $RecoveryIntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn),
    $recoveryTrigger
)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

if ($PSCmdlet.ShouldProcess($TaskName, "Register public edge route watchdog task")) {
    if ($CurrentUser) {
        $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $userTriggers = @(
            (New-ScheduledTaskTrigger -AtLogOn -User $currentUser),
            $recoveryTrigger
        )
        $userPrincipal = New-ScheduledTaskPrincipal `
            -UserId $currentUser `
            -LogonType Interactive `
            -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $userTriggers `
            -Settings $settings `
            -Principal $userPrincipal `
            -Description "ETA public edge route watchdog. Explicit current-user fallback with recurring route checks." `
            -Force | Out-Null
        $principalLabel = "current_user:$currentUser, AtLogOn+every-$RecoveryIntervalMinutes" + "m"
    } else {
        $currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
        $isAdmin = $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $isAdmin) {
            throw "Administrator rights are required to register $TaskName as SYSTEM. Use repair_eta_public_edge_route_watchdog_admin.cmd or rerun from an elevated shell."
        }
        $systemPrincipal = New-ScheduledTaskPrincipal `
            -UserId "NT AUTHORITY\SYSTEM" `
            -LogonType ServiceAccount `
            -RunLevel Highest
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $triggers `
            -Settings $settings `
            -Principal $systemPrincipal `
            -Description "ETA public edge route watchdog. Probes 127.0.0.1:8081 against 127.0.0.1:8421 and repairs FirmCommandCenterEdge route drift." `
            -Force | Out-Null
        $principalLabel = "SYSTEM, AtStartup+AtLogOn+every-$RecoveryIntervalMinutes" + "m"
    }

    if ($Start) {
        Start-ScheduledTask -TaskName $TaskName
    }

    Get-ScheduledTask -TaskName $TaskName |
        Select-Object TaskName,State,@{Name="UserId";Expression={$_.Principal.UserId}},@{Name="PrincipalLabel";Expression={$principalLabel}},@{Name="PythonExe";Expression={$Python}},@{Name="RecoveryIntervalMinutes";Expression={$RecoveryIntervalMinutes}}
}
