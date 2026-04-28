# Register the 16 operator-tooling scheduled tasks on the VPS.
#
# These are tasks that ran on the operator's laptop pre-rebrand. Now that
# everything is going 24/7 on the VPS (operator mandate 2026-04-26 — no
# cmd prompts on home machine), they need to be re-registered against the
# VPS-side install paths and run hidden so the VPS console stays clean.
#
# Each task uses:
#   * Hidden: $true                     (no console flash)
#   * StartWhenAvailable                (catch up after VPS reboot)
#   * MultipleInstances=IgnoreNew       (don't pile up if a tick is slow)
#   * RunLevel=Limited (default user)   (no UAC prompt)
#
# Idempotent: re-running unregisters + recreates each task.
#
# Used by: vps_supercharge_bootstrap.ps1
#
# Manual usage:
#   powershell -File register_operator_tasks.ps1                   # register, enabled
#   powershell -File register_operator_tasks.ps1 -KeepDisabled     # register, disabled (cutover-safe)
#   powershell -File register_operator_tasks.ps1 -DryRun           # show what WOULD register
#   powershell -File register_operator_tasks.ps1 -OnlyTask mnq_daily_pipeline  # one task only
[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\",
    [string]$EtaEngineDir = "C:\eta_engine",
    [string]$MnqBacktestDir = "C:\mnq_backtest",
    [string]$MnqEtaBotDir = "C:\mnq_eta_bot",
    [string]$JarvisIdentityDir = "C:\jarvis_identity",
    [string]$Python = "C:\Python314\python.exe",
    [string]$VenvPython = "C:\mnq_backtest\.venv\Scripts\python.exe",
    [switch]$KeepDisabled,
    [switch]$DryRun,
    [string]$OnlyTask = ""
)

$ErrorActionPreference = "Continue"

# Resolve venv python: fall back to system python if venv doesn't exist
function Resolve-VenvPython {
    if (Test-Path $VenvPython) { return $VenvPython }
    return $Python
}
$VenvPy = Resolve-VenvPython

# --- task spec table --------------------------------------------------
# Triggers:
#   "Daily-HHMM"             -> daily at HH:MM local
#   "Weekday-HHMM"           -> Mon-Fri at HH:MM
#   "Weekly-DAY-HHMM"        -> weekly on DAY at HH:MM (DAY in {Sunday..Saturday})
#   "EveryNMin"              -> every N minutes
#   "RthEveryNMin"           -> every N minutes weekday from 09:30 ET for 6h30m
#
# All tasks register with Hidden + StartWhenAvailable so the VPS catches
# up after reboots and never flashes a console window.
$tasks = @(
    @{
        Name       = "BatmanWorker"
        Exec       = $Python
        Args       = "`"$JarvisIdentityDir\batman\batman_worker.py`""
        Cwd        = $JarvisIdentityDir
        Trigger    = "Every1Min"
        Notes      = "LLM persona worker (Batman fleet) -- every 1 min"
    },
    @{
        Name       = "EtaIbkrBbo1mCapture"
        Exec       = $Python
        Args       = "`"$MnqBacktestDir\scripts\ibkr_bbo1m_capture.py`""
        Cwd        = $MnqBacktestDir
        Trigger    = "Every5Min"
        Notes      = "IBKR L1+BBO 1-min data capture"
    },
    @{
        Name       = "EtaTier2SnapshotSync"
        Exec       = $Python
        Args       = "`"$JarvisIdentityDir\scripts\sync_tier2_snapshot.py`""
        Cwd        = $JarvisIdentityDir
        Trigger    = "Every30Min"
        Notes      = "Tier-2 snapshot sync"
    },
    @{
        Name       = "FirmApp-PaperOpen"
        Exec       = "powershell.exe"
        Args       = "-NoProfile -ExecutionPolicy Bypass -File `"$EtaEngineDir\firm_paper_open.ps1`""
        Cwd        = $EtaEngineDir
        Trigger    = "Weekly-Sunday-1800"
        Notes      = "Sunday 18:00 ET CME futures reopen, flips bot SIM->PAPER"
    },
    @{
        Name       = "firm_dashboard_daily"
        Exec       = $Python
        Args       = "`"$MnqBacktestDir\scripts\generate_firm_dashboard.py`""
        Cwd        = $MnqBacktestDir
        Trigger    = "Daily-0015"
        Notes      = "Daily 00:15 firm dashboard generator"
    },
    @{
        Name       = "firm_paper_replay_daily"
        Exec       = $Python
        Args       = "`"$MnqBacktestDir\scripts\paper_replay_simulator.py`""
        Cwd        = $MnqBacktestDir
        Trigger    = "Daily-0025"
        Notes      = "Daily 00:25 paper-replay simulator"
    },
    @{
        Name       = "firm_regression_daily"
        Exec       = $Python
        Args       = "`"$MnqBacktestDir\scripts\eta_engine_nightly_regression.py`""
        Cwd        = $MnqBacktestDir
        Trigger    = "Daily-0020"
        Notes      = "Daily 00:20 ETA Engine nightly regression"
    },
    @{
        Name       = "TheFirm-DailyDigest"
        Exec       = "cmd.exe"
        Args       = "/c cd /d $MnqBacktestDir && `"$Python`" scripts\cron_daily_digest.py"
        Cwd        = $MnqBacktestDir
        Trigger    = "Daily-1830"
        Notes      = "Daily 18:30 firm digest"
    },
    @{
        Name       = "mnq_daily_digest"
        Exec       = "$MnqBacktestDir\scripts\cron_daily_digest.bat"
        Args       = ""
        Cwd        = $MnqBacktestDir
        Trigger    = "Weekday-1617"
        Notes      = "Weekday 16:17 MNQ daily digest -> Discord"
    },
    @{
        Name       = "mnq_daily_pipeline"
        Exec       = "$MnqBacktestDir\scripts\cron_daily_pipeline.bat"
        Args       = ""
        Cwd        = $MnqBacktestDir
        Trigger    = "Weekday-1630"
        Notes      = "Weekday 16:30 MNQ pipeline (post-cash-close)"
    },
    @{
        Name       = "mnq_daily_sim_paper"
        Exec       = $VenvPy
        Args       = "`"$MnqBacktestDir\scripts\daily_sim_paper.py`" --bars 500"
        Cwd        = $MnqBacktestDir
        Trigger    = "Weekday-1700"
        Notes      = "Weekday 17:00 MNQ sim paper run"
    },
    @{
        Name       = "mnq_tv_monitor_rth"
        Exec       = "$MnqBacktestDir\scripts\cron_tv_monitor_rth.bat"
        Args       = ""
        Cwd        = $MnqBacktestDir
        Trigger    = "Weekday-0925"
        Notes      = "Weekday 09:25 TradingView signal monitor (RTH session). NOTE: needs TV access from VPS -- if TV is desktop-only, leave this on local instead."
    },
    @{
        Name       = "mnq_walk_forward_drift"
        Exec       = "$MnqBacktestDir\scripts\cron_walk_forward_drift.bat"
        Args       = ""
        Cwd        = $MnqBacktestDir
        Trigger    = "Weekly-Sunday-2247"
        Notes      = "Sunday 22:47 walk-forward drift check"
    },
    @{
        Name       = "MNQ_Eta_Heartbeat"
        Exec       = "$MnqEtaBotDir\run_heartbeat.bat"
        Args       = ""
        Cwd        = $MnqEtaBotDir
        Trigger    = "RthEvery10Min"
        Notes      = "Every 10 min during RTH (09:30-16:00 ET) -- bot dead-bot detector"
    },
    @{
        Name       = "MNQ_Eta_Readiness"
        Exec       = "$MnqEtaBotDir\run_readiness.bat"
        Args       = ""
        Cwd        = $MnqEtaBotDir
        Trigger    = "Weekly-Monday-0830"
        Notes      = "Monday 08:30 readiness check"
    },
    @{
        Name       = "MNQ_Eta_Shadow"
        Exec       = "$MnqEtaBotDir\run_shadow.bat"
        Args       = ""
        Cwd        = $MnqEtaBotDir
        Trigger    = "Weekly-Sunday-1900"
        Notes      = "Sunday 19:00 post-gamma shadow harness"
    },
    # Lever 1 (kaizen, 2026-04-26): close the daily cycle, produce the +1 ticket.
    @{
        Name       = "Eta-Kaizen-DailyClose"
        Exec       = $Python
        Args       = "-m eta_engine.scripts.run_kaizen_close_cycle"
        Cwd        = $EtaEngineDir
        Trigger    = "Daily-2230"
        Notes      = "Daily 22:30 -- close kaizen cycle + produce mandatory +1 ticket. Doctrine: every cycle MUST emit a ticket."
    },
    # Lever 7 (regime-shift alert, 2026-04-26): denial-rate watcher.
    @{
        Name       = "Eta-Jarvis-DenialRate"
        Exec       = $Python
        Args       = "-m eta_engine.obs.jarvis_denial_rate_alerter"
        Cwd        = $EtaEngineDir
        Trigger    = "Every1Min"
        Notes      = "Every 1 min -- if JARVIS denial rate >= 50% for 5+ min, fire Resend alert (with cooldown)"
    },
    # Tier-1 #2 (2026-04-27): position reconciler.
    @{
        Name       = "Eta-Position-Reconciler"
        Exec       = $Python
        Args       = "-m eta_engine.obs.position_reconciler"
        Cwd        = $EtaEngineDir
        Trigger    = "Every1Min"
        Notes      = "Every 1 min -- compare bot-internal positions vs broker; fire position_drift on mismatch."
    },
    # Tier-3 #9 (2026-04-27): daemon auto-recovery (heartbeat-based deadlock kill).
    @{
        Name       = "Eta-Daemon-Recovery"
        Exec       = $Python
        Args       = "-m eta_engine.obs.daemon_recovery_watchdog"
        Cwd        = $EtaEngineDir
        Trigger    = "Every1Min"
        Notes      = "Every 1 min -- kill deadlocked daemons (heartbeat stale > 3x cadence); Task Scheduler restarts."
    },
    # Tier-3 #12 (2026-04-27): quarterly archive cleanup.
    @{
        Name       = "Eta-Archive-Cleanup-Quarterly"
        Exec       = $Python
        Args       = "-m eta_engine.scripts.auto_archive_cleanup --max-age-days 90"
        Cwd        = $EtaEngineDir
        Trigger    = "Weekly-Sunday-0345"
        Notes      = "Sunday 03:45 -- compress + remove _archive_*/ dirs older than 90 days."
    },
    # Tier-4 #15 (2026-04-27): investor / inner-circle / beta dashboard.
    @{
        Name       = "Eta-Investor-Dashboard-Daily"
        Exec       = $Python
        Args       = "-m eta_engine.scripts.generate_investor_dashboard"
        Cwd        = $EtaEngineDir
        Trigger    = "Daily-2300"
        Notes      = "Daily 23:00 -- regenerate investor/beta dashboard HTML at state/investor_dashboard/index.html."
    },
    # Wave-4 (2026-04-27): critique nightly (BATMAN 2nd reviewer)
    @{
        Name       = "Eta-Critique-Nightly"
        Exec       = $Python
        Args       = "-m eta_engine.scripts.run_critique_nightly"
        Cwd        = $EtaEngineDir
        Trigger    = "Daily-2245"
        Notes      = "Daily 22:45 -- run critique_window over the day's audit; alerts on HIGH severity."
    },
    # Wave-4: calibration fit
    @{
        Name       = "Eta-Calibration-Daily"
        Exec       = $Python
        Args       = "-m eta_engine.scripts.run_calibration_fit"
        Cwd        = $EtaEngineDir
        Trigger    = "Daily-2300"
        Notes      = "Daily 23:00 -- fit Platt sigmoid from last 14 days of audit; persist calibrator."
    },
    # Wave-4: anomaly scan every 15 min
    @{
        Name       = "Eta-Anomaly-Scan-15m"
        Exec       = $Python
        Args       = "-m eta_engine.scripts.run_anomaly_scan"
        Cwd        = $EtaEngineDir
        Trigger    = "Every15Min"
        Notes      = "Every 15 min -- KS-test on verdict stress distribution; alert on regime shift."
    },
    # Wave-4: bandit promotion check (daily)
    @{
        Name       = "Eta-Bandit-Promotion-Daily"
        Exec       = $Python
        Args       = "-m eta_engine.scripts.bandit_promotion_check"
        Cwd        = $EtaEngineDir
        Trigger    = "Daily-2330"
        Notes      = "Daily 23:30 -- score every candidate vs champion over last 30d; alert on promotable."
    },
    # Wave-4: verdict webhook tail (every 1 min)
    @{
        Name       = "Eta-Verdict-Webhook"
        Exec       = $Python
        Args       = "-m eta_engine.obs.jarvis_verdict_webhook"
        Cwd        = $EtaEngineDir
        Trigger    = "Every1Min"
        Notes      = "Every 1 min -- forward DENIED verdicts (default) to ETA_VERDICT_WEBHOOK_URL (Slack/Discord)."
    },
    # Wave-6 pre-live (2026-04-27): sage on-chain cache warmer for crypto bots
    @{
        Name       = "Eta-Sage-OnChain-Warm"
        Exec       = $Python
        Args       = "-m eta_engine.scripts.sage_onchain_warm --symbols BTCUSDT,ETHUSDT"
        Cwd        = $EtaEngineDir
        Trigger    = "Every5Min"
        Notes      = "Every 5 min -- pre-fetch BTC + ETH on-chain metrics (mempool.space, defillama, coingecko) so the OnChainSchool sees warm data when crypto bots ask sage."
    },
    # Wave-6 pre-live: sage health watchdog (silently-broken school detector)
    @{
        Name       = "Eta-Sage-Health-Daily"
        Exec       = $Python
        Args       = "-m eta_engine.scripts.sage_health_check --json-out state/sage/last_health_report.json"
        Cwd        = $EtaEngineDir
        Trigger    = "Daily-2315"
        Notes      = "Daily 23:15 -- check every school's neutral_rate; alert on critical (>=95% neutral over >=30 consultations). Writes snapshot for the dashboard."
    },
    # Wave-7 (2026-04-27): Stage 1 cutover -- new dashboard auto-launch on 8420.
    @{
        Name       = "Eta-Dashboard"
        Exec       = $Python
        Args       = "-m uvicorn eta_engine.deploy.scripts.dashboard_api:app --host 127.0.0.1 --port 8420"
        Cwd        = $EtaEngineDir
        Trigger    = "AtStartup"
        Notes      = "Wave-7 dashboard: serves the JARVIS command center + bot fleet view at http://127.0.0.1:8420/. Replaces the firm command_center on 8420 (firm command_center kept in repo until Stage 2 decommission)."
    }
)

# --- helpers ----------------------------------------------------------
function New-EtaTrigger([string]$spec) {
    $maxDur = New-TimeSpan -Days 9999
    switch -Regex ($spec) {
        '^AtStartup$' {
            # Boot-time trigger -- fires once when the VPS comes up.
            return New-ScheduledTaskTrigger -AtStartup
        }
        '^Every(\d+)Min$' {
            $n = [int]$matches[1]
            return New-ScheduledTaskTrigger -Once -At (Get-Date) `
                -RepetitionInterval (New-TimeSpan -Minutes $n) `
                -RepetitionDuration $maxDur
        }
        '^Daily-(\d{2})(\d{2})$' {
            $h = [int]$matches[1]; $m = [int]$matches[2]
            return New-ScheduledTaskTrigger -Daily -At (Get-Date -Hour $h -Minute $m -Second 0)
        }
        '^Weekday-(\d{2})(\d{2})$' {
            $h = [int]$matches[1]; $m = [int]$matches[2]
            return New-ScheduledTaskTrigger -Weekly `
                -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
                -At (Get-Date -Hour $h -Minute $m -Second 0)
        }
        '^Weekly-(\w+)-(\d{2})(\d{2})$' {
            $day = $matches[1]; $h = [int]$matches[2]; $m = [int]$matches[3]
            return New-ScheduledTaskTrigger -Weekly -DaysOfWeek $day `
                -At (Get-Date -Hour $h -Minute $m -Second 0)
        }
        '^RthEvery(\d+)Min$' {
            $n = [int]$matches[1]
            # 09:30 ET, every N min, for 6h30m, weekdays
            return New-ScheduledTaskTrigger -Weekly `
                -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
                -At (Get-Date -Hour 9 -Minute 30 -Second 0) `
                -RepetitionInterval (New-TimeSpan -Minutes $n) `
                -RepetitionDuration (New-TimeSpan -Hours 6 -Minutes 30)
        }
    }
    throw "unknown trigger spec: $spec"
}

function Register-EtaOpTask([hashtable]$t) {
    $name = $t.Name
    Write-Host "  -- $name" -ForegroundColor Cyan
    Write-Host "       trigger:  $($t.Trigger)"
    Write-Host "       exec:     $($t.Exec) $($t.Args)"
    Write-Host "       cwd:      $($t.Cwd)"
    if ($DryRun) { Write-Host "       (DryRun) skipping registration"; return }

    # Drop existing first so we can re-create idempotently
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue

    # Build action
    if ([string]::IsNullOrEmpty($t.Cwd)) {
        $action = if ([string]::IsNullOrEmpty($t.Args)) {
            New-ScheduledTaskAction -Execute $t.Exec
        } else {
            New-ScheduledTaskAction -Execute $t.Exec -Argument $t.Args
        }
    } else {
        $action = if ([string]::IsNullOrEmpty($t.Args)) {
            New-ScheduledTaskAction -Execute $t.Exec -WorkingDirectory $t.Cwd
        } else {
            New-ScheduledTaskAction -Execute $t.Exec -Argument $t.Args -WorkingDirectory $t.Cwd
        }
    }

    $trigger = New-EtaTrigger -spec $t.Trigger
    $settings = New-ScheduledTaskSettingsSet `
        -Hidden $true `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -User $env:USERNAME -RunLevel Limited `
        -Description $t.Notes | Out-Null

    if ($KeepDisabled) {
        Disable-ScheduledTask -TaskName $name | Out-Null
        Write-Host "       state:    DISABLED (per -KeepDisabled)" -ForegroundColor Yellow
    } else {
        Write-Host "       state:    READY" -ForegroundColor Green
    }
}

# --- main loop --------------------------------------------------------
Write-Host "[register-operator-tasks] EtaEngineDir=$EtaEngineDir"
Write-Host "[register-operator-tasks] MnqBacktestDir=$MnqBacktestDir"
Write-Host "[register-operator-tasks] MnqEtaBotDir=$MnqEtaBotDir"
Write-Host "[register-operator-tasks] JarvisIdentityDir=$JarvisIdentityDir"
Write-Host "[register-operator-tasks] Python=$Python"
Write-Host "[register-operator-tasks] VenvPython=$VenvPy"
Write-Host ""

$registered = 0
$skipped = 0
foreach ($t in $tasks) {
    if ($OnlyTask -and ($t.Name -ne $OnlyTask)) { $skipped++; continue }
    Register-EtaOpTask -t $t
    $registered++
}

Write-Host ""
Write-Host "=== summary ==="
Write-Host "  registered: $registered tasks"
if ($skipped -gt 0) { Write-Host "  skipped:    $skipped (filter: -OnlyTask=$OnlyTask)" }
Write-Host "  state:      $(if ($KeepDisabled) { 'DISABLED' } else { 'READY' })"
Write-Host ""
Write-Host "  All tasks run hidden (no console flash). Verify in Task Scheduler:"
Write-Host "    Get-ScheduledTask -TaskName 'BatmanWorker','EtaIbkr*','firm_*','mnq_*','MNQ_Eta_*','TheFirm-DailyDigest','FirmApp-PaperOpen' | Select TaskName,State"
