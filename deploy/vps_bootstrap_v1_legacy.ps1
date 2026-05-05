# VPS Bootstrap — one-command full stack startup
# Wave-18 (2026-04-30): Registers all DeepSeek services, Hermes bridge,
# autonomous kaizen, quantum daily rebalance, and health check.
#
# Usage from VPS host (as Administrator):
#   pwsh -ExecutionPolicy Bypass -File .\vps_bootstrap.ps1
#
# Or for a dry run:
#   pwsh -ExecutionPolicy Bypass -File .\vps_bootstrap.ps1 -WhatIf

param(
    [string]$InstallRoot = "C:\EvolutionaryTradingAlgo",
    [string]$EtaEngineDir = "",
    [string]$FirmDir = "",
    [switch]$SkipHermes,
    [switch]$SkipKaizen,
    [switch]$SkipQuantum,
    [switch]$SkipHealthCheck,
    [switch]$SkipDeepSeekTick,
    [switch]$SkipIbkrGateway,
    [switch]$SkipAllServices,
    [switch]$WhatIf
)

$ErrorActionPreference = "Continue"
$pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonExe) {
    $pythonExe = "$InstallRoot\eta_engine\.venv\Scripts\python.exe"
}
if (-not (Test-Path $pythonExe)) {
    Write-Error "Python not found at $pythonExe"
    Write-Host "Check: uv sync --locked --extra dev in eta_engine/" -ForegroundColor Yellow
    if (-not $WhatIf) { exit 1 }
}

if (-not $EtaEngineDir) { $EtaEngineDir = "$InstallRoot\eta_engine" }
if (-not $FirmDir) { $FirmDir = "$InstallRoot\firm\eta_engine" }
$pwshPath = (Get-Command powershell -ErrorAction SilentlyContinue).Source
if (-not $pwshPath) { $pwshPath = "powershell.exe" }

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  ETA VPS Bootstrap — Full Stack Registration" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "Install root: $InstallRoot" -ForegroundColor Gray
Write-Host "Python:       $pythonExe" -ForegroundColor Gray
Write-Host ""

# ── Verify secrets ────────────────────────────────────────

$secretsDir = "$InstallRoot\secrets"
$fccSecretsDir = "$InstallRoot\firm_command_center\secrets"
$tokenFile = "$secretsDir\telegram_bot_token.txt"
$chatFile = "$secretsDir\telegram_chat_id.txt"
$quantumCreds = "$secretsDir\quantum_creds.json"

if (-not (Test-Path $secretsDir)) {
    New-Item -ItemType Directory -Force -Path $secretsDir | Out-Null
}

if (-not (Test-Path $tokenFile)) {
    Write-Host "[SECRETS] telegram_bot_token.txt missing" -ForegroundColor Yellow
    if (-not $WhatIf) {
        Set-Content -Path $tokenFile -Value "# Place your Telegram bot token here" -Encoding UTF8
    }
}
if (-not (Test-Path $chatFile)) {
    Write-Host "[SECRETS] telegram_chat_id.txt missing" -ForegroundColor Yellow
    if (-not $WhatIf) {
        Set-Content -Path $chatFile -Value "# Place your Telegram chat ID here" -Encoding UTF8
    }
}
if (-not (Test-Path $quantumCreds)) {
    $credsTemplate = '{"_comment":"Place D-Wave/IBM Quantum credentials here","dwave":{"token":"","solver":"Advantage_system4.1","region":"na-west-1"},"ibm":{"token":"","instance":"ibm-q/open/main","backend":"ibm_kyiv"},"budget":{"max_cost_per_job_usd":0.50,"max_cost_per_day_usd":5.00,"enable_cloud":false}}'
    Write-Host "[SECRETS] quantum_creds.json created (fill API keys before enabling cloud)" -ForegroundColor Yellow
    if (-not $WhatIf) {
        $credsTemplate | Out-File $quantumCreds -Encoding UTF8
    }
}

# Sync secrets to firm_command_center (Hermes service reads from here)
if (-not (Test-Path $fccSecretsDir)) {
    New-Item -ItemType Directory -Force -Path $fccSecretsDir | Out-Null
}
if ((Test-Path $tokenFile) -and -not (Test-Path "$fccSecretsDir\telegram_bot_token.txt")) {
    Copy-Item $tokenFile "$fccSecretsDir\" -Force
}
if ((Test-Path $chatFile) -and -not (Test-Path "$fccSecretsDir\telegram_chat_id.txt")) {
    Copy-Item $chatFile "$fccSecretsDir\" -Force
}
Write-Host "[SECRETS] Synced to $fccSecretsDir for Hermes service" -ForegroundColor Gray

if (-not $SkipHermes) {
    Write-Host ""; Write-Host "=== Hermes Telegram Bridge ===" -ForegroundColor Green
    $hermesScript = "$InstallRoot\firm\eta_engine\deploy\windows\install_hermes_service.ps1"
    if (Test-Path $hermesScript) {
        Write-Host "  Registering Hermes service..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $hermesScript `
                -InstallRoot $InstallRoot -SkipTaskScheduler
        }
    } else {
        Write-Host "  Hermes install script not found at $hermesScript" -ForegroundColor Yellow
    }
}

# ── Health check scheduled task ───────────────────────────

if (-not $SkipHealthCheck) {
    Write-Host ""; Write-Host "=== Health Check ===" -ForegroundColor Green
    $healthScript = "$EtaEngineDir\scripts\health_check.py"

    if (Test-Path $healthScript) {
        $taskName = "ETA-HealthCheck"

        # Run once now
        Write-Host "  Running health check..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pythonExe $healthScript 2>&1 | Select-Object -Last 5
        }

        # Register daily task
        $action = New-ScheduledTaskAction -Execute $pythonExe `
            -Argument "`"$healthScript`" --output-dir `"$InstallRoot\firm_command_center\var\health`""
        $trigger = New-ScheduledTaskTrigger -Daily -At "08:00" -RepetitionInterval (New-TimeSpan -Hours 4) -RepetitionDuration (New-TimeSpan -Hours 24)
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

        if (-not $WhatIf) {
            Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
            Write-Host "  Registered: $taskName (every 4h)" -ForegroundColor Green
        }
    }
}

# ── Quantum daily rebalance ───────────────────────────────

if (-not $SkipQuantum) {
    Write-Host ""; Write-Host "=== Quantum Daily Rebalance ===" -ForegroundColor Green
    $quantumScript = "$EtaEngineDir\scripts\quantum_daily_rebalance.py"

    if (Test-Path $quantumScript) {
        $taskName = "ETA-Quantum-Daily-Rebalance"
        $action = New-ScheduledTaskAction -Execute $pythonExe `
            -Argument "`"$quantumScript`" --instruments MNQ,BTC,ETH,SOL --out-dir `"$InstallRoot\var\eta_engine\state\quantum`""
        $trigger = New-ScheduledTaskTrigger -Daily -At "21:00"
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

        if (-not $WhatIf) {
            Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
            Write-Host "  Registered: $taskName (daily at 21:00 UTC)" -ForegroundColor Green
        }
    }
}

# ── DeepSeek + Codex scheduler ticks ─────────────────────

if (-not $SkipDeepSeekTick) {
    Write-Host ""; Write-Host "=== DeepSeek + Codex Scheduler ===" -ForegroundColor Green
    $registerScript = "$FirmDir\..\scripts\Register-DeepSeekScheduledTasks.ps1"

    if (Test-Path $registerScript) {
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $registerScript
        }
    } elseif (Test-Path "$FirmDir\..\..\scripts\Register-DeepSeekScheduledTasks.ps1") {
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File "$FirmDir\..\..\scripts\Register-DeepSeekScheduledTasks.ps1"
        }
    } else {
        Write-Host "  Register-DeepSeekScheduledTasks.ps1 not found — skipping" -ForegroundColor Yellow
    }
}

# ── IBKR Gateway Watchdog ──────────────────────────────

if (-not $SkipIbkrGateway) {
    Write-Host ""; Write-Host "=== IBKR Gateway Watchdog ===" -ForegroundColor Green
    $ibkrWatchdogScript = "$FirmDir\deploy\windows\register_ibkr_gateway_watchdog_task.ps1"

    if (Test-Path $ibkrWatchdogScript) {
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $ibkrWatchdogScript `
                -ApexRoot "$InstallRoot\firm_command_center" `
                -RunNow
            Write-Host "  Registered: ETAIbkrGatewayWatchdog (every 5m, auto-start on boot)" -ForegroundColor Green
        }
    } else {
        Write-Host "  register_ibkr_gateway_watchdog_task.ps1 not found at $ibkrWatchdogScript — skipping" -ForegroundColor Yellow
    }
}

# ── Final health check ───────────────────────────────────

Write-Host ""; Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  BOOTSTRAP COMPLETE" -ForegroundColor Green
Write-Host "====================================================" -ForegroundColor Cyan

if (-not $WhatIf) {
    Write-Host "Running final health check..." -ForegroundColor Gray
    $healthScript = "$EtaEngineDir\scripts\health_check.py"
    if (Test-Path $healthScript) {
        & $pythonExe $healthScript 2>&1 | Select-Object -Last 15
    }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        Write-Host "Health: GREEN" -ForegroundColor Green
    } elseif ($exitCode -eq 1) {
        Write-Host "Health: YELLOW — check action items above" -ForegroundColor Yellow
    } else {
        Write-Host "Health: RED — critical issues detected" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "Scheduled tasks:" -ForegroundColor White
Write-Host "  ETA-HealthCheck               — every 4h" -ForegroundColor Gray
Write-Host "  ETA-Quantum-Daily-Rebalance    — daily 21:00 UTC" -ForegroundColor Gray
Write-Host "  ETA-DeepSeek-MachineGate       — every 2h" -ForegroundColor Gray
Write-Host "  ETA-DeepSeek-CodexLane         — every 4h" -ForegroundColor Gray
Write-Host "  ETA-Hermes-Jarvis-Flush        — daily 00:30 UTC" -ForegroundColor Gray
Write-Host ""
Write-Host "Secrets needed:" -ForegroundColor White
Write-Host "  secrets/telegram_bot_token.txt  — for Hermes Telegram push" -ForegroundColor Gray
Write-Host "  secrets/telegram_chat_id.txt    — for Hermes Telegram push" -ForegroundColor Gray
Write-Host "  secrets/quantum_creds.json      — for cloud quantum (D-Wave/IBM)" -ForegroundColor Gray
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Fill in secrets files" -ForegroundColor Gray
Write-Host "  2. Start Hermes: Start-Service HermesJarvisTelegram" -ForegroundColor Gray
Write-Host "  3. Verify: Get-ScheduledTask ETA-* | Select TaskName, State" -ForegroundColor Gray
Write-Host "  4. Monitor: tail -f firm_command_center/var/health/current_health.json" -ForegroundColor Gray
