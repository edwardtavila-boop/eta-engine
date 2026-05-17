$ErrorActionPreference = "Stop"

$taskName = "ETA-Quantum-Daily-Rebalance"
$workspaceRoot = "C:\EvolutionaryTradingAlgo"
$pythonExe = "C:\Python314\python.exe"
$quantumScript = Join-Path $workspaceRoot "eta_engine\scripts\quantum_daily_rebalance.py"

if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found at $pythonExe"
}
if (-not (Test-Path $quantumScript)) {
    throw "Canonical quantum_daily_rebalance.py not found at $quantumScript"
}

$trigger = New-ScheduledTaskTrigger -Daily -At 9:00PM

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$action = New-ScheduledTaskAction `
    -Execute $pythonExe `
    -Argument "`"$quantumScript`" --instruments MNQ,BTC,ETH,SOL" `
    -WorkingDirectory $workspaceRoot

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$description = "ETA: canonical quantum daily rebalance from C:\EvolutionaryTradingAlgo\eta_engine\scripts\quantum_daily_rebalance.py"

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
