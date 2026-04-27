# VPS supercharge bootstrap -- one-shot script the operator runs ONCE on
# the VPS to migrate everything from the home laptop to 24/7 VPS operation.
#
# Operator mandate 2026-04-26: "everything 24/7 on the VPS, no cmd prompts
# opening up on home computer". This script:
#
#   1. Verifies prerequisites (git, gh, python, eta_engine repo present)
#   2. Optionally renames C:\apex_predator\ -> C:\eta_engine\ (post-rebrand)
#   3. Clones the 3 satellite repos (mnq_backtest, mnq_eta_bot, jarvis_identity)
#   4. Registers the 16 operator-tooling scheduled tasks (hidden, idempotent)
#   5. Optionally also registers the persona/fleet daemons via register_tasks.ps1
#   6. Prints a summary of what's now running on the VPS
#
# Usage on the VPS (as Administrator):
#
#     # First time:
#     cd C:\eta_engine                     # (or wherever the repo lives)
#     git pull                              # get latest deploy/scripts/
#     powershell -File deploy\scripts\vps_supercharge_bootstrap.ps1
#
#     # Cutover-safe (register tasks but leave disabled, enable manually later):
#     powershell -File deploy\scripts\vps_supercharge_bootstrap.ps1 -KeepDisabled
#
#     # Skip clone if you've already cloned the satellite repos:
#     powershell -File deploy\scripts\vps_supercharge_bootstrap.ps1 -SkipClone
#
#     # Dry run (see what WOULD happen):
#     powershell -File deploy\scripts\vps_supercharge_bootstrap.ps1 -DryRun
[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\",
    [string]$GitHubOwner = "edwardtavila-boop",
    [string]$EtaEngineDir = "C:\eta_engine",
    [switch]$SkipClone,
    [switch]$SkipTasks,
    [switch]$KeepDisabled,
    [switch]$IncludeFleetDaemons,   # also run register_tasks.ps1 (persona daemons)
    [switch]$IncludeTradingView,    # also install TradingView + register AtLogOn task
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $PSCommandPath

function Write-Banner([string]$text) {
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host "================================================================" -ForegroundColor Cyan
}

# --- 1. pre-flight ----------------------------------------------------
Write-Banner "1/5  pre-flight checks"

$prereqs = @{
    git    = "Required to clone the satellite repos"
    gh     = "Optional but recommended (gh auth setup-git)"
    python = "Required for the bot scripts"
}
foreach ($cmd in $prereqs.Keys) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found) {
        Write-Host "  OK   $cmd  ($($found.Source))" -ForegroundColor Green
    } else {
        Write-Host "  MISS $cmd  ($($prereqs[$cmd]))" -ForegroundColor Yellow
    }
}

# Verify eta_engine is at the expected location
if (-not (Test-Path $EtaEngineDir)) {
    Write-Host ""
    Write-Host "  WARN: $EtaEngineDir does not exist." -ForegroundColor Yellow
    # Older deploys put it at C:\apex_predator\ -- offer to rename it.
    if (Test-Path "C:\apex_predator") {
        Write-Host "  Found C:\apex_predator -- looks like a pre-rebrand install."
        if (-not $DryRun) {
            $resp = Read-Host "  Rename C:\apex_predator -> C:\eta_engine ? [y/N]"
            if ($resp -match '^[yY]') {
                # Stop any process holding files in the old location
                Get-Process | Where-Object {
                    try { $_.Path -and $_.Path.StartsWith("C:\apex_predator\") } catch { $false }
                } | Stop-Process -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 1
                Rename-Item -LiteralPath "C:\apex_predator" -NewName "eta_engine" -Force
                Write-Host "  RENAMED C:\apex_predator -> C:\eta_engine" -ForegroundColor Green
            }
        }
    } else {
        Write-Host "  Clone the eta_engine repo first:"
        Write-Host "    git clone https://github.com/$GitHubOwner/eta-engine.git $EtaEngineDir"
        exit 1
    }
}

# ---2. clone satellite repos ────────────────────────────────────────
if ($SkipClone) {
    Write-Banner "2/5  satellite repos -- SKIPPED (per -SkipClone)"
} else {
    Write-Banner "2/5  satellite repos (mnq_backtest, mnq_eta_bot, jarvis_identity)"
    $cloneScript = Join-Path $ScriptDir "clone_satellite_repos.ps1"
    if (Test-Path $cloneScript) {
        $cloneArgs = @("-File", $cloneScript, "-InstallRoot", $InstallRoot, "-GitHubOwner", $GitHubOwner)
        if ($DryRun) { $cloneArgs += "-DryRun" }
        & powershell.exe @cloneArgs
    } else {
        Write-Host "  ERROR: $cloneScript not found" -ForegroundColor Red
    }
}

# ---3. register operator tasks ──────────────────────────────────────
if ($SkipTasks) {
    Write-Banner "3/5  operator tasks -- SKIPPED (per -SkipTasks)"
} else {
    Write-Banner "3/5  16 operator-tooling scheduled tasks"
    $taskScript = Join-Path $ScriptDir "register_operator_tasks.ps1"
    if (Test-Path $taskScript) {
        $taskArgs = @(
            "-File", $taskScript,
            "-InstallRoot", $InstallRoot,
            "-EtaEngineDir", $EtaEngineDir,
            "-MnqBacktestDir", (Join-Path $InstallRoot "mnq_backtest"),
            "-MnqEtaBotDir",  (Join-Path $InstallRoot "mnq_eta_bot"),
            "-JarvisIdentityDir", (Join-Path $InstallRoot "jarvis_identity")
        )
        if ($KeepDisabled) { $taskArgs += "-KeepDisabled" }
        if ($DryRun) { $taskArgs += "-DryRun" }
        & powershell.exe @taskArgs
    } else {
        Write-Host "  ERROR: $taskScript not found" -ForegroundColor Red
    }
}

# ---4. optionally register fleet daemons ────────────────────────────
if ($IncludeFleetDaemons) {
    Write-Banner "4/5  fleet/persona daemons (Apex/Eta-Robin, -Alfred, -Batman, -Jarvis)"
    $fleetScript = Join-Path $ScriptDir "register_tasks.ps1"
    if (Test-Path $fleetScript) {
        & powershell.exe -File $fleetScript -InstallDir $EtaEngineDir
    } else {
        Write-Host "  ERROR: $fleetScript not found" -ForegroundColor Red
    }
} else {
    Write-Banner "4/5  fleet daemons -- skipped (use -IncludeFleetDaemons to run register_tasks.ps1 too)"
}

# --- 4b. optionally install TradingView Desktop + AtLogOn task -------
if ($IncludeTradingView) {
    Write-Banner "4b/5  TradingView Desktop install + AtLogOn launcher"
    $tvScript = Join-Path $ScriptDir "install_tradingview_vps.ps1"
    if (Test-Path $tvScript) {
        $tvArgs = @("-File", $tvScript)
        if ($DryRun) { $tvArgs += "-DryRun" }
        & powershell.exe @tvArgs
    } else {
        Write-Host "  ERROR: $tvScript not found" -ForegroundColor Red
    }
} else {
    Write-Banner "4b/5  TradingView -- skipped (use -IncludeTradingView for mnq_tv_monitor_rth on VPS)"
}

# ---5. summary ──────────────────────────────────────────────────────
Write-Banner "5/5  final state"

Write-Host ""
Write-Host "Repos under ${InstallRoot}:"
foreach ($d in @("eta_engine", "mnq_backtest", "mnq_eta_bot", "jarvis_identity")) {
    $p = Join-Path $InstallRoot $d
    if (Test-Path (Join-Path $p ".git")) {
        Push-Location $p
        try {
            $sha = (& git rev-parse --short HEAD 2>$null).Trim()
            Write-Host ("  OK   {0,-20} HEAD={1}" -f $d, $sha) -ForegroundColor Green
        } finally { Pop-Location }
    } else {
        Write-Host ("  MISS {0}" -f $d) -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Scheduled tasks:"
$opTasks = @(
    "BatmanWorker", "EtaIbkrBbo1mCapture", "EtaTier2SnapshotSync",
    "FirmApp-PaperOpen",
    "firm_dashboard_daily", "firm_paper_replay_daily", "firm_regression_daily",
    "TheFirm-DailyDigest",
    "mnq_daily_digest", "mnq_daily_pipeline", "mnq_daily_sim_paper",
    "mnq_tv_monitor_rth", "mnq_walk_forward_drift",
    "MNQ_Eta_Heartbeat", "MNQ_Eta_Readiness", "MNQ_Eta_Shadow"
)
$registeredCount = 0
foreach ($name in $opTasks) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($t) {
        $registeredCount++
        Write-Host ("  {0,-30} {1}" -f $name, $t.State)
    }
}
Write-Host ""
Write-Host "  $registeredCount / $($opTasks.Count) operator tasks registered"
if ($IncludeFleetDaemons) {
    $fleetCount = (Get-ScheduledTask -TaskName "Apex-*","Eta-*" -ErrorAction SilentlyContinue | Measure-Object).Count
    Write-Host "  $fleetCount fleet/persona tasks registered"
}

Write-Host ""
Write-Host "Done. Next:" -ForegroundColor Cyan
Write-Host "  1. Populate .env files in each repo (creds, API keys)"
Write-Host "  2. If -KeepDisabled was used, enable tasks individually:"
Write-Host "     Enable-ScheduledTask -TaskName BatmanWorker"
Write-Host "  3. Verify Resend alerts still work:"
Write-Host "     curl https://jarvis.apexpredator.live/api/alert/test"
Write-Host "  4. Disable the corresponding tasks on your home laptop:"
Write-Host "     Get-ScheduledTask -TaskName 'BatmanWorker','EtaIbkr*','firm_*','mnq_*','MNQ_Eta_*','TheFirm-*','FirmApp-*' | Disable-ScheduledTask"
Write-Host ""
