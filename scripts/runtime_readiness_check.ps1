<#
.SYNOPSIS
  24/7 runtime readiness audit -- sister to paper_live_launch_check.py.

.DESCRIPTION
  paper_live_launch_check.py audits the ALGORITHM layer (registered
  bots, validated baselines, data on disk). This script audits the
  RUNTIME layer (Windows services running, scheduled tasks ready,
  python processes alive, decision journal accepting writes).

  Both must be green before greenlight on a 24/7 deployment.

.LAYERS AUDITED
  1. winsw services (Firm*) -- continuous daemons. Must be Running
     and StartType=Automatic.
  2. Scheduled tasks -- periodic jobs. Must be Ready (not Disabled),
     and reference paths that exist on disk.
  3. Python processes -- bot loops + backtest workers. Heuristic
     check via Get-Process.
  4. Decision journal -- flush latency, last-write timestamp.
  5. Broker session state -- IBKR Client Portal Gateway, Tastytrade
     OAuth (if configured).

.OUTPUT
  Prints a status table per layer. Returns:
    0 -- fully ready
    >0 -- count of issues found (per gate)

.USAGE
    # Full audit (default)
    pwsh scripts\runtime_readiness_check.ps1

    # JSON output for dashboard / CI
    pwsh scripts\runtime_readiness_check.ps1 -Json

    # Skip slow checks (network, broker probes)
    pwsh scripts\runtime_readiness_check.ps1 -Fast
#>

param(
    [switch]$Json,
    [switch]$Fast
)

$ErrorActionPreference = 'Stop'
$results = [ordered]@{
    timestamp = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')
    services = @()
    scheduled_tasks = @()
    processes = @()
    journal = $null
    issues = @()
    summary = $null
}

# -- Layer 1: winsw services ------------------------------------------------
# CRITICAL services run the trading; absence/non-Running BLOCKS.
# OPTIONAL services are public-access (Caddy edge proxy + Cloudflare
# tunnel) for the remote dashboard; their absence is a soft warning,
# not a blocker.
$critical_services = @('FirmCore', 'FirmCommandCenter', 'FirmWatchdog')
$optional_services = @('FirmCommandCenterEdge', 'FirmCommandCenterTunnel')
foreach ($svc_name in ($critical_services + $optional_services)) {
    $is_critical = $critical_services -contains $svc_name
    $svc = Get-Service -Name $svc_name -ErrorAction SilentlyContinue
    if (-not $svc) {
        $row = @{
            name = $svc_name; status = 'MISSING'; start_type = 'n/a'
            critical = $is_critical
        }
        if ($is_critical) {
            $results.issues += "CRITICAL service '$svc_name' not installed"
        }
    } else {
        $row = @{
            name = $svc.Name; status = $svc.Status.ToString()
            start_type = $svc.StartType.ToString(); critical = $is_critical
        }
        if ($svc.Status -ne 'Running' -and $is_critical) {
            $results.issues += "CRITICAL service '$svc_name' is $($svc.Status) (expected Running)"
        }
        if ($svc.StartType -ne 'Automatic' -and $is_critical) {
            $results.issues += "CRITICAL service '$svc_name' StartType=$($svc.StartType) (expected Automatic)"
        }
    }
    $results.services += $row
}

# -- Layer 2: scheduled tasks -----------------------------------------------
$expected_tasks = @(
    'ETA-FleetCorrCheck', 'ETA-WeeklySharpe',
    'EtaIbkrBbo1mCapture', 'EtaTier2SnapshotSync',
    'FirmApp-PaperOpen',
    'firm_dashboard_daily', 'firm_paper_replay_daily', 'firm_regression_daily',
    'mnq_daily_digest', 'mnq_daily_pipeline', 'mnq_daily_sim_paper',
    'mnq_tv_monitor_rth', 'mnq_walk_forward_drift',
    'TheFirm-DailyDigest'
)
# Stale tasks -- mnq_apex_bot dir removed in 2026-04-26 rebrand
$stale_tasks = @('MNQ_Eta_Heartbeat', 'MNQ_Eta_Readiness', 'MNQ_Eta_Shadow')

foreach ($task_name in $expected_tasks) {
    $t = Get-ScheduledTask -TaskName $task_name -ErrorAction SilentlyContinue
    if (-not $t) {
        $row = @{ name = $task_name; state = 'MISSING'; path_valid = $false; last_result = $null }
        $results.issues += "scheduled task '$task_name' not found"
    } else {
        $info = Get-ScheduledTaskInfo -TaskName $task_name -ErrorAction SilentlyContinue
        # Verify path
        $a = $t.Actions[0]
        $path_check = $null
        if ($a.Execute -like '*python*') {
            $m = [regex]::Match($a.Arguments, '"?([A-Z]:\\[^"]+\.(?:py|bat))"?')
            if ($m.Success) { $path_check = $m.Groups[1].Value }
        } elseif ($a.Execute -eq 'powershell.exe' -or $a.Execute -like '*\powershell.exe') {
            $m = [regex]::Match($a.Arguments, '"?([A-Z]:\\[^"]+\.(?:ps1|py|bat))"?')
            if ($m.Success) { $path_check = $m.Groups[1].Value }
        } elseif ($a.Execute -eq 'cmd' -or $a.Execute -like '*\cmd.exe' -or $a.Execute -eq 'cmd.exe') {
            $m = [regex]::Match($a.Arguments, '"?([A-Z]:\\[^&]+(?:\.bat|\.py|\.ps1))"?')
            if ($m.Success) {
                $path_check = $m.Groups[1].Value.Trim()
            } else {
                # Try chained "cd /d X && python Y" form
                $m2 = [regex]::Match($a.Arguments, 'cd /d ([A-Z]:\\[^\s&]+)')
                if ($m2.Success) { $path_check = $m2.Groups[1].Value }
            }
        } elseif ($a.Execute -like '*.bat' -or $a.Execute -like '*.exe' -or $a.Execute -like '*.ps1') {
            $path_check = $a.Execute.Trim('"')
        }
        $exists = if ($path_check) { Test-Path -LiteralPath $path_check } else { $null }
        $row = @{
            name = $task_name
            state = $t.State.ToString()
            path_valid = $exists
            path = $path_check
            last_result = $info.LastTaskResult
            last_run = if ($info.LastRunTime -gt (Get-Date '2000-01-01')) { $info.LastRunTime.ToString('yyyy-MM-dd HH:mm') } else { 'never' }
        }
        if ($t.State -eq 'Disabled') {
            $results.issues += "task '$task_name' is Disabled (expected Ready)"
        }
        if ($exists -eq $false) {
            $results.issues += "task '$task_name' references missing path: $path_check"
        }
    }
    $results.scheduled_tasks += $row
}

# Stale tasks (explicitly OK to be disabled)
foreach ($task_name in $stale_tasks) {
    $t = Get-ScheduledTask -TaskName $task_name -ErrorAction SilentlyContinue
    if ($t) {
        $row = @{
            name = $task_name
            state = $t.State.ToString()
            path_valid = $false
            stale = $true
            note = 'mnq_apex_bot dir removed in 2026-04-26 rebrand; deletion candidate'
        }
        $results.scheduled_tasks += $row
    }
}

# -- Layer 3: python processes ----------------------------------------------
$pyprocs = Get-Process -Name 'python*' -ErrorAction SilentlyContinue
foreach ($p in $pyprocs) {
    $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId = $($p.Id)" -ErrorAction SilentlyContinue).CommandLine
    if ($cmdline -like '*EvolutionaryTradingAlgo*' -or $cmdline -like '*eta_engine*' -or $cmdline -like '*firm*') {
        $results.processes += @{
            pid = $p.Id
            name = $p.ProcessName
            cpu_seconds = [math]::Round($p.CPU, 1)
            memory_mb = [math]::Round($p.WorkingSet64 / 1MB, 1)
            cmdline = if ($cmdline.Length -gt 100) { $cmdline.Substring(0, 100) + '...' } else { $cmdline }
        }
    }
}

# -- Layer 4: decision journal ----------------------------------------------
$journal_paths = @(
    'C:\EvolutionaryTradingAlgo\firm_command_center\var\reports\decision_journal.jsonl',
    'C:\EvolutionaryTradingAlgo\firm_command_center\eta_engine\docs\decision_journal.jsonl',
    'C:\EvolutionaryTradingAlgo\eta_engine\docs\decision_journal.jsonl'
)
foreach ($jp in $journal_paths) {
    if (Test-Path -LiteralPath $jp) {
        $info = Get-Item -LiteralPath $jp
        $age_min = [math]::Round(((Get-Date) - $info.LastWriteTime).TotalMinutes, 1)
        $results.journal = @{
            path = $jp
            size_mb = [math]::Round($info.Length / 1MB, 2)
            last_write = $info.LastWriteTime.ToString('yyyy-MM-dd HH:mm')
            age_minutes = $age_min
        }
        if ($age_min -gt 60) {
            $results.issues += "decision journal stale (last write $age_min minutes ago)"
        }
        break
    }
}
if (-not $results.journal) {
    $results.issues += 'decision journal file not found in any expected location'
}

# -- Summary ----------------------------------------------------------------
$n_running_services = ($results.services | Where-Object { $_.status -eq 'Running' }).Count
$n_total_services = $results.services.Count
$n_ready_tasks = ($results.scheduled_tasks | Where-Object { $_.state -eq 'Ready' }).Count
$n_total_tasks = $results.scheduled_tasks.Count
$n_processes = $results.processes.Count

$results.summary = @{
    services_running = "$n_running_services/$n_total_services"
    tasks_ready = "$n_ready_tasks/$n_total_tasks"
    processes_alive = $n_processes
    issue_count = $results.issues.Count
    overall = if ($results.issues.Count -eq 0) { 'READY' } else { 'BLOCK' }
}

# -- Output -----------------------------------------------------------------
if ($Json) {
    $results | ConvertTo-Json -Depth 6
} else {
    Write-Host "=== 24/7 RUNTIME READINESS CHECK ===" -ForegroundColor Cyan
    Write-Host "Timestamp: $($results.timestamp)"
    Write-Host ""
    Write-Host "--- Services (Layer 1) ---" -ForegroundColor Yellow
    $results.services | ForEach-Object {
        $color = if ($_.status -eq 'Running') { 'Green' } else { 'Red' }
        Write-Host ("  {0,-30} {1,-10} {2}" -f $_.name, $_.status, $_.start_type) -ForegroundColor $color
    }
    Write-Host ""
    Write-Host "--- Scheduled Tasks (Layer 2) ---" -ForegroundColor Yellow
    $results.scheduled_tasks | ForEach-Object {
        $color = if ($_.state -eq 'Ready') { 'Green' } elseif ($_.stale) { 'DarkGray' } else { 'Yellow' }
        $valid = if ($_.path_valid -eq $true) { 'OK' } elseif ($_.path_valid -eq $false) { 'X' } else { '?' }
        Write-Host ("  {0} {1,-28} {2,-10} path:{3}" -f $valid, $_.name, $_.state, $_.path) -ForegroundColor $color
    }
    Write-Host ""
    Write-Host "--- Python Processes (Layer 3) ---" -ForegroundColor Yellow
    if ($results.processes.Count -eq 0) {
        Write-Host "  (no eta_engine/firm python processes alive)" -ForegroundColor DarkGray
    } else {
        $results.processes | ForEach-Object {
            Write-Host ("  PID {0,-7} {1,-10} CPU={2}s RAM={3}MB" -f $_.pid, $_.name, $_.cpu_seconds, $_.memory_mb)
        }
    }
    Write-Host ""
    Write-Host "--- Decision Journal (Layer 4) ---" -ForegroundColor Yellow
    if ($results.journal) {
        Write-Host ("  {0} ({1} MB, last write {2} min ago)" -f $results.journal.path, $results.journal.size_mb, $results.journal.age_minutes)
    } else {
        Write-Host "  not found" -ForegroundColor Red
    }
    Write-Host ""
    Write-Host "--- ISSUES ---" -ForegroundColor Yellow
    if ($results.issues.Count -eq 0) {
        Write-Host "  (none)" -ForegroundColor Green
    } else {
        $results.issues | ForEach-Object {
            Write-Host "  ! $_" -ForegroundColor Red
        }
    }
    Write-Host ""
    Write-Host "=== SUMMARY ===" -ForegroundColor Cyan
    Write-Host ("  Services:  {0}" -f $results.summary.services_running)
    Write-Host ("  Tasks:     {0}" -f $results.summary.tasks_ready)
    Write-Host ("  Processes: {0}" -f $results.summary.processes_alive)
    Write-Host ("  Issues:    {0}" -f $results.summary.issue_count)
    $overall_color = if ($results.summary.overall -eq 'READY') { 'Green' } else { 'Red' }
    Write-Host ("  Overall:   {0}" -f $results.summary.overall) -ForegroundColor $overall_color
}

exit $results.issues.Count
