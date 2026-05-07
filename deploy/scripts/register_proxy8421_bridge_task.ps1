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
$runner = Join-Path $Root "deploy\scripts\run_proxy8421_task.cmd"
$workspaceRoot = Split-Path -Parent $Root
$logDir = Join-Path $workspaceRoot "logs\eta_engine"

if (-not (Test-Path $bridge)) {
    throw "Missing reverse proxy bridge: $bridge"
}

if (-not (Test-Path $runner)) {
    throw "Missing proxy8421 task runner: $runner"
}

& $python -m py_compile $bridge
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { ([string]$_.CommandLine) -match "reverse_proxy_bridge\.py" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Get-CimInstance Win32_Process -Filter "name='cmd.exe'" -ErrorAction SilentlyContinue |
    Where-Object { ([string]$_.CommandLine) -match "run_proxy8421_task\.cmd|reverse_proxy_bridge\.py" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$action = New-ScheduledTaskAction -Execute $runner -WorkingDirectory $Root
$triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task for ${ListenHost}:${ListenPort} -> ${Target}")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $principal `
        -Description "ETA compatibility bridge for Cloudflare remote ops route: ${ListenHost}:${ListenPort} -> ${Target}" `
        -Force | Out-Null

    if ($Start) {
        Start-ScheduledTask -TaskName $TaskName
    }

    Get-ScheduledTask -TaskName $TaskName |
        Select-Object TaskName,State,@{Name="UserId";Expression={$_.Principal.UserId}},@{Name="LogonType";Expression={$_.Principal.LogonType}},@{Name="Runner";Expression={$runner}},@{Name="Python";Expression={$python}}
} else {
    [pscustomobject]@{
        TaskName = $TaskName
        State = "WhatIf"
        UserId = "SYSTEM"
        LogonType = "ServiceAccount"
        Runner = $runner
        Python = $python
        Target = $Target
        Listen = "${ListenHost}:${ListenPort}"
    }
}
