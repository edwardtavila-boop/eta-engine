 [CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "ETA-Proxy-8421",
    [string]$Root = "C:\EvolutionaryTradingAlgo\eta_engine",
    [string]$Python = "",
    [string]$ListenHost = "127.0.0.1",
    [int]$ListenPort = 8421,
    [string]$Target = "http://127.0.0.1:8000",
    [int]$TimeoutSec = 60,
    [switch]$Start
)

$ErrorActionPreference = "Stop"

function Resolve-PythonRuntime {
    param(
        [string]$Root,
        [string]$ExplicitPython
    )

    if ($ExplicitPython) {
        if (-not (Test-Path $ExplicitPython)) {
            throw "Missing explicit Python runtime: $ExplicitPython"
        }
        return (Resolve-Path $ExplicitPython).Path
    }

    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        return (Resolve-Path $venvPython).Path
    }

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source) {
        return $pythonCommand.Source
    }

    $pyCommand = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyCommand -and $pyCommand.Source) {
        return $pyCommand.Source
    }

    throw "Missing Python runtime: no .venv python under $Root and no python.exe/py.exe on PATH"
}

$python = Resolve-PythonRuntime -Root $Root -ExplicitPython $Python
$bridge = Join-Path $Root "deploy\scripts\reverse_proxy_bridge.py"

if (-not (Test-Path $bridge)) {
    throw "Missing reverse proxy bridge: $bridge"
}

& $python -m py_compile $bridge

$arguments = '"{0}" --listen-host {1} --listen-port {2} --target {3} --timeout {4}' -f `
    $bridge, $ListenHost, $ListenPort, $Target, $TimeoutSec

$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task for ${ListenHost}:${ListenPort} -> ${Target}")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "ETA compatibility bridge for Cloudflare remote ops route: ${ListenHost}:${ListenPort} -> ${Target}" `
        -Force | Out-Null

    if ($Start) {
        Start-ScheduledTask -TaskName $TaskName
    }

    Get-ScheduledTask -TaskName $TaskName |
        Select-Object TaskName,State,@{Name="UserId";Expression={$_.Principal.UserId}},@{Name="LogonType";Expression={$_.Principal.LogonType}},@{Name="Python";Expression={$python}}
} else {
    [pscustomobject]@{
        TaskName = $TaskName
        State = "WhatIf"
        UserId = "SYSTEM"
        LogonType = "ServiceAccount"
        Python = $python
        Target = $Target
        Listen = "${ListenHost}:${ListenPort}"
    }
}
