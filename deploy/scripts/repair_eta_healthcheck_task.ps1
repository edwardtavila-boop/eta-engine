[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$taskName = "ETA-HealthCheck"
$workspaceRoot = "C:\EvolutionaryTradingAlgo"
$pythonExe = "C:\Python314\python.exe"
$healthScript = Join-Path $workspaceRoot "eta_engine\scripts\health_check.py"
$healthOutDir = Join-Path $workspaceRoot "firm_command_center\var\health"

if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found at $pythonExe"
}
if (-not (Test-Path $healthScript)) {
    throw "Canonical health_check.py not found at $healthScript"
}
if (-not (Test-Path $healthOutDir)) {
    New-Item -ItemType Directory -Path $healthOutDir -Force | Out-Null
}

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 4) `
    -RepetitionDuration (New-TimeSpan -Days 365)

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$action = New-ScheduledTaskAction `
    -Execute $pythonExe `
    -Argument "`"$healthScript`" --allow-remote-supervisor-truth --allow-remote-retune-truth --output-dir `"$healthOutDir`"" `
    -WorkingDirectory $workspaceRoot

$description = "ETA: canonical health check every 4h from C:\EvolutionaryTradingAlgo\eta_engine\scripts\health_check.py with remote-truth allowances and firm_command_center health output"

if ($DryRun) {
    [pscustomobject]@{
        task_name = $taskName
        execute = $pythonExe
        arguments = $action.Arguments
        working_directory = $workspaceRoot
        trigger = "once_plus_4h_repeat_365d"
        output_dir = $healthOutDir
        remote_truth_flags = @(
            "--allow-remote-supervisor-truth",
            "--allow-remote-retune-truth"
        )
        description = $description
    } | Format-List
    return
}

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

try {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $description `
        | Out-Null
}
catch {
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal `
        -UserId $currentUser `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "$description (current-user fallback)" `
        | Out-Null
}

Start-ScheduledTask -TaskName $taskName

$registered = Get-ScheduledTask -TaskName $taskName
$registered.Actions | Format-List Execute,Arguments,WorkingDirectory
