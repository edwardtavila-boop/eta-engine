<#
.SYNOPSIS
  Idempotent re-enable for the 12 verified ETA scheduled tasks.

.DESCRIPTION
  After the 2026-04-26 rebrand, 14 scheduled tasks were left
  Disabled pending verification. This script:

  1. Verifies each task's command path exists on disk
  2. Re-enables ONLY tasks with valid paths (idempotent -- safe to
     re-run; already-Ready tasks are skipped)
  3. Reports stale tasks (3 of them, all referencing the deleted
     mnq_apex_bot dir) -- does NOT delete them by default; pass
     -DeleteStale to remove them

  Run this AFTER running runtime_readiness_check.ps1 and confirming
  the audit looks good.

.USAGE
    # Default: enable verified tasks, leave stale tasks alone
    pwsh scripts\reenable_eta_tasks.ps1

    # Dry-run -- show what would change without changing anything
    pwsh scripts\reenable_eta_tasks.ps1 -DryRun

    # Also delete the 3 stale mnq_apex_bot tasks
    pwsh scripts\reenable_eta_tasks.ps1 -DeleteStale

.NOTES
  Requires Administrator privileges to enable/delete tasks.
#>

param(
    [switch]$DryRun,
    [switch]$DeleteStale
)

$ErrorActionPreference = 'Stop'

# Tasks verified to have valid paths (per runtime_readiness_check audit)
$enable_tasks = @(
    'EtaIbkrBbo1mCapture',
    'EtaTier2SnapshotSync',
    'FirmApp-PaperOpen',
    'firm_dashboard_daily',
    'firm_paper_replay_daily',
    'firm_regression_daily',
    'mnq_daily_digest',
    'mnq_daily_pipeline',
    'mnq_daily_sim_paper',
    'mnq_tv_monitor_rth',
    'mnq_walk_forward_drift',
    'TheFirm-DailyDigest'
)

# Tasks referencing the removed mnq_apex_bot dir
$stale_tasks = @(
    'MNQ_Eta_Heartbeat',
    'MNQ_Eta_Readiness',
    'MNQ_Eta_Shadow'
)

Write-Host "=== ETA Scheduled Task Re-enable ===" -ForegroundColor Cyan
if ($DryRun) {
    Write-Host "DRY RUN -- no changes will be made" -ForegroundColor Yellow
}
Write-Host ""

$enabled = 0
$skipped_already_ready = 0
$failed = 0

foreach ($name in $enable_tasks) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $t) {
        Write-Host "  ? $name -- task not found, skipping" -ForegroundColor DarkGray
        continue
    }
    if ($t.State -ne 'Disabled') {
        Write-Host "  - $name -- already $($t.State), skipping" -ForegroundColor DarkGray
        $skipped_already_ready++
        continue
    }
    # Re-verify path before enabling (defensive -- paths can drift)
    $a = $t.Actions[0]
    $path_check = $null
    if ($a.Execute -like '*python*') {
        $m = [regex]::Match($a.Arguments, '"?([A-Z]:\\[^"]+\.(?:py|bat))"?')
        if ($m.Success) { $path_check = $m.Groups[1].Value }
    } elseif ($a.Execute -eq 'powershell.exe' -or $a.Execute -like '*\powershell.exe') {
        # Powershell -File "X.ps1" -- check arguments BEFORE the generic .exe branch
        $m = [regex]::Match($a.Arguments, '"?([A-Z]:\\[^"]+\.(?:ps1|py|bat))"?')
        if ($m.Success) { $path_check = $m.Groups[1].Value }
    } elseif ($a.Execute -eq 'cmd' -or $a.Execute -like '*\cmd.exe' -or $a.Execute -eq 'cmd.exe') {
        $m = [regex]::Match($a.Arguments, 'cd /d ([A-Z]:\\[^\s&]+)')
        if ($m.Success) { $path_check = $m.Groups[1].Value }
    } elseif ($a.Execute -like '*.bat' -or $a.Execute -like '*.exe' -or $a.Execute -like '*.ps1') {
        $path_check = $a.Execute.Trim('"')
    }
    $path_valid = if ($path_check) { Test-Path -LiteralPath $path_check } else { $false }
    if (-not $path_valid) {
        Write-Host "  X $name -- path no longer valid: $path_check" -ForegroundColor Red
        $failed++
        continue
    }
    if ($DryRun) {
        Write-Host "  -> $name -- would enable (path: $path_check)" -ForegroundColor Yellow
    } else {
        try {
            Enable-ScheduledTask -TaskName $name | Out-Null
            Write-Host "  OK $name -- ENABLED" -ForegroundColor Green
            $enabled++
        } catch {
            Write-Host "  X $name -- Enable failed: $_" -ForegroundColor Red
            $failed++
        }
    }
}

Write-Host ""
Write-Host "--- Stale tasks (mnq_apex_bot dir removed) ---" -ForegroundColor Yellow
$stale_handled = 0
foreach ($name in $stale_tasks) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $t) {
        Write-Host "  - $name -- already deleted" -ForegroundColor DarkGray
        continue
    }
    if ($DeleteStale) {
        if ($DryRun) {
            Write-Host "  -> $name -- would DELETE" -ForegroundColor Yellow
        } else {
            try {
                Unregister-ScheduledTask -TaskName $name -Confirm:$false
                Write-Host "  OK $name -- DELETED" -ForegroundColor Green
                $stale_handled++
            } catch {
                Write-Host "  X $name -- delete failed: $_" -ForegroundColor Red
            }
        }
    } else {
        Write-Host "  ! $name -- STALE (left disabled; pass -DeleteStale to remove)" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "=== SUMMARY ===" -ForegroundColor Cyan
if ($DryRun) {
    Write-Host "  DRY RUN -- no changes made"
} else {
    Write-Host "  Enabled:                   $enabled"
    Write-Host "  Already ready (skipped):   $skipped_already_ready"
    Write-Host "  Failed:                    $failed" -ForegroundColor $(if ($failed -gt 0) { 'Red' } else { 'White' })
    Write-Host "  Stale tasks handled:       $stale_handled"
}

if ($failed -gt 0) { exit 1 }
exit 0
