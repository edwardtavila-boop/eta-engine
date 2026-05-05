# Register Codex + three-AI coordination tasks on the Windows VPS.
#
# These tasks are safe by default: they write operator reports and AI
# coordination state only under C:\EvolutionaryTradingAlgo\var\eta_engine.

[CmdletBinding()]
param(
    [string]$InstallDir = "C:\EvolutionaryTradingAlgo\eta_engine",
    [string]$StateDir = "",
    [string]$LogDir = "",
    [string]$PythonExe = "",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

$workspaceRoot = Split-Path -Parent $InstallDir
if (-not $StateDir) {
    $StateDir = Join-Path $workspaceRoot "var\eta_engine\state"
}
if (-not $LogDir) {
    $LogDir = Join-Path $workspaceRoot "logs\eta_engine"
}
if (-not $PythonExe) {
    $venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        $PythonExe = $venvPython
    } else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $cmd) {
            throw "Python not found. Create .venv in $InstallDir or put python on PATH."
        }
        $PythonExe = $cmd.Source
    }
}

$coordinationState = Join-Path $StateDir "agent_coordination"
$operatorReportDir = Join-Path $StateDir "codex_operator"
$operatorScript = Join-Path $InstallDir "scripts\codex_overnight_operator.py"
$threeAiSyncScript = Join-Path $InstallDir "scripts\three_ai_sync.py"

New-Item -ItemType Directory -Force -Path $coordinationState | Out-Null
New-Item -ItemType Directory -Force -Path $operatorReportDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function New-RepeatingTrigger([int]$Minutes) {
    $duration = New-TimeSpan -Days 9999
    return New-ScheduledTaskTrigger `
        -Once `
        -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $Minutes) `
        -RepetitionDuration $duration
}

function Register-ETATask($Name, $ScriptPath, $Arguments, $IntervalMinutes) {
    if (-not (Test-Path $ScriptPath)) {
        throw "Required script missing: $ScriptPath"
    }

    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue

    $action = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument "`"$ScriptPath`" $Arguments" `
        -WorkingDirectory $InstallDir
    $trigger = New-RepeatingTrigger -Minutes $IntervalMinutes
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -User $env:USERNAME `
        -RunLevel Limited | Out-Null

    Write-Host "[ OK ] $Name registered every $IntervalMinutes minutes" -ForegroundColor Green

    if ($RunNow) {
        Start-ScheduledTask -TaskName $Name
        Write-Host "[ OK ] $Name started" -ForegroundColor Green
    }
}

$codexArgs = @(
    "--workspace-root `"$workspaceRoot`"",
    "--eta-engine-root `"$InstallDir`"",
    "--state-root `"$coordinationState`"",
    "--report-dir `"$operatorReportDir`""
) -join " "

Register-ETATask `
    -Name "ETA-Codex-Overnight-Operator" `
    -ScriptPath $operatorScript `
    -Arguments $codexArgs `
    -IntervalMinutes 10

Register-ETATask `
    -Name "ETA-ThreeAI-Sync" `
    -ScriptPath $threeAiSyncScript `
    -Arguments "" `
    -IntervalMinutes 240

Write-Host "[ETA-codex] State:  $coordinationState" -ForegroundColor Cyan
Write-Host "[ETA-codex] Report: $operatorReportDir\codex_operator_latest.json" -ForegroundColor Cyan
