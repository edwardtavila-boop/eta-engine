# Install TradingView Desktop on the VPS and wire it to launch at logon.
#
# Operator mandate (2026-04-26): the mnq_tv_monitor_rth task polls
# TradingView Desktop -- so for the VPS to take that task over from the
# laptop, TradingView Desktop has to live there too.
#
# This script:
#   1. Installs TradingView Desktop via winget if not already installed
#      (falls back to a manual-download prompt if winget is unavailable
#      or the package id has changed)
#   2. Registers an AtLogOn scheduled task "Eta-TradingView-AtLogon" that
#      launches TradingView when the VPS user logs in
#   3. Sets sensible window flags (minimized, no UAC prompt)
#
# Note: TradingView is a GUI app -- it needs an interactive desktop
# session to render. The VPS must either:
#   (a) have auto-logon configured for the Administrator account, OR
#   (b) keep an RDP session connected (the session has to be active
#       for the GUI to be alive)
# The scraper (tv_signal_monitor_daemon.py) attaches to the TV window
# and reads symbols/signals through it.
#
# After install, you must MANUALLY log into TradingView ONCE (so it
# remembers your credentials). The AtLogOn task will relaunch TV
# pre-authenticated on every logon thereafter.
[CmdletBinding()]
param(
    [string]$WingetId = "TradingView.TradingView",
    [switch]$SkipInstall,           # only register the AtLogOn task
    [switch]$SkipTask,              # only install, no AtLogOn task
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"

function Write-Step([string]$text) {
    Write-Host ""
    Write-Host "[install-tradingview] $text" -ForegroundColor Cyan
}

# --- 1. detect existing install --------------------------------------
Write-Step "1/3  detect existing TradingView install"
$tvCandidates = @(
    "$env:LOCALAPPDATA\TradingView\TradingView.exe",
    "$env:ProgramFiles\TradingView\TradingView.exe",
    "${env:ProgramFiles(x86)}\TradingView\TradingView.exe"
)
$tvExe = $tvCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($tvExe) {
    Write-Host "  OK   TradingView already installed at: $tvExe" -ForegroundColor Green
} else {
    Write-Host "  not installed yet"
}

# --- 2. install via winget if needed ---------------------------------
if (-not $SkipInstall -and -not $tvExe) {
    Write-Step "2/3  install TradingView Desktop"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Host "  ERROR: winget not on PATH." -ForegroundColor Red
        Write-Host "  On Windows Server, install winget from https://aka.ms/getwinget"
        Write-Host "  Or download TradingView Desktop manually from:"
        Write-Host "    https://www.tradingview.com/desktop/"
        Write-Host "  Then re-run this script with -SkipInstall"
        exit 1
    }

    Write-Host "  installing $WingetId via winget..."
    if ($DryRun) {
        Write-Host "  (DryRun) winget install --id $WingetId -e --accept-source-agreements --accept-package-agreements"
    } else {
        # --silent prevents UAC prompts on most installers; some still
        # require an admin token. -e = exact match on package id.
        $wingetArgs = @(
            "install",
            "--id", $WingetId,
            "-e",
            "--accept-source-agreements",
            "--accept-package-agreements",
            "--silent"
        )
        & winget @wingetArgs 2>&1 | ForEach-Object { Write-Host "    $_" }
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  WARN: winget install returned exit $LASTEXITCODE (may be already installed)" -ForegroundColor Yellow
        }
    }

    # Re-detect
    $tvExe = $tvCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($tvExe) {
        Write-Host "  OK   installed at: $tvExe" -ForegroundColor Green
    } else {
        Write-Host "  WARN: could not locate TradingView.exe after install" -ForegroundColor Yellow
        Write-Host "  Check: $tvCandidates"
    }
} elseif ($SkipInstall) {
    Write-Step "2/3  install -- SKIPPED (per -SkipInstall)"
}

# --- 3. register AtLogOn scheduled task ------------------------------
if (-not $SkipTask) {
    Write-Step "3/3  register Eta-TradingView-AtLogon task"
    if (-not $tvExe) {
        Write-Host "  ERROR: cannot find TradingView.exe to schedule." -ForegroundColor Red
        Write-Host "  Re-run with -SkipInstall once TradingView is installed."
        exit 1
    }

    $taskName = "Eta-TradingView-AtLogon"

    if ($DryRun) {
        Write-Host "  (DryRun) would register $taskName -> $tvExe"
    } else {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

        # Action: launch TV. Use cmd /c start with /MIN so it opens
        # minimized rather than stealing focus from any other apps.
        $action = New-ScheduledTaskAction `
            -Execute "cmd.exe" `
            -Argument "/c start `"`" /MIN `"$tvExe`""

        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

        # NOT hidden -- TV needs to render its window for the scraper.
        # Delay 30s so we don't fight the rest of the AtLogOn boot rush.
        $trigger.Delay = "PT30S"

        $settings = New-ScheduledTaskSettingsSet `
            -StartWhenAvailable `
            -DontStopIfGoingOnBatteries `
            -AllowStartIfOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -MultipleInstances IgnoreNew

        Register-ScheduledTask `
            -TaskName $taskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -User $env:USERNAME `
            -RunLevel Limited `
            -Description "Launch TradingView Desktop at logon (minimized) so mnq_tv_monitor_rth has a TV instance to scrape." | Out-Null

        Write-Host "  OK   $taskName registered (AtLogOn, 30s delay, minimized)" -ForegroundColor Green
    }
} elseif ($SkipTask) {
    Write-Step "3/3  AtLogOn task -- SKIPPED (per -SkipTask)"
}

# --- 4. one-time human steps -----------------------------------------
Write-Host ""
Write-Host "=== ONE-TIME human steps after install ===" -ForegroundColor Yellow
Write-Host ""
Write-Host "  1. Launch TradingView ONCE manually (Start menu -> TradingView)"
Write-Host "  2. Sign in with your TV account"
Write-Host "  3. Check 'Remember me' so TV restores the session on next launch"
Write-Host "  4. Configure a chart layout that the scraper expects (CME_MINI:MNQ1!"
Write-Host "     for the mnq_tv_monitor_rth task)"
Write-Host "  5. Close TV"
Write-Host "  6. Log out + log back in -- the AtLogOn task should auto-relaunch TV"
Write-Host "     and you should see the chart layout preserved"
Write-Host ""
Write-Host "After that, mnq_tv_monitor_rth on this VPS will work the same as it"
Write-Host "did on the laptop -- TV is up, scraper attaches, signals flow through."
