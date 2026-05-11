$ErrorActionPreference = "Continue"
foreach ($name in @("ETA-IBGateway", "ETA-IBGateway-DailyRestart", "ETA-IBGateway-Reauth", "ETA-TWS-Watchdog")) {
    Write-Host "=========================================="
    Write-Host "TASK: $name"
    Write-Host "=========================================="
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $t) {
        Write-Host "  NOT FOUND"
        continue
    }
    Write-Host "  State: $($t.State)"
    foreach ($a in $t.Actions) {
        Write-Host "  Action.Execute  : $($a.Execute)"
        Write-Host "  Action.Arguments: $($a.Arguments)"
        Write-Host "  Action.WorkDir  : $($a.WorkingDirectory)"
    }
    foreach ($trig in $t.Triggers) {
        Write-Host "  Trigger: $($trig.GetType().Name)"
    }
    $info = $t | Get-ScheduledTaskInfo
    Write-Host "  LastRunTime    : $($info.LastRunTime)"
    Write-Host "  LastTaskResult : 0x$($info.LastTaskResult.ToString('X'))"
    Write-Host "  NextRunTime    : $($info.NextRunTime)"
    Write-Host ""
}
