# VPS Bootstrap â€” one-command full stack startup
# Wave-19 (2026-05-04): WinSW service registration, ETA persona tasks,
# Force Multiplier CLI checks, dual venv validation, IBKR path fix.
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
    [switch]$SkipWinSW,
    [switch]$SkipETATasks,
    [switch]$SkipForceMultiplier,
    [switch]$SkipQuantum,
    [switch]$SkipHealthCheck,
    [switch]$SkipCodexOperator,
    [switch]$SkipDeepSeekTick,
    [switch]$SkipIbkrGateway,
    [switch]$SkipAllServices,
    [switch]$WhatIf
)

$ErrorActionPreference = "Continue"

# â”€â”€ Path resolution â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if (-not $EtaEngineDir) { $EtaEngineDir = "$InstallRoot\eta_engine" }
if (-not $FirmDir) { $FirmDir = "$InstallRoot\firm\eta_engine" }
$venvPython = "$EtaEngineDir\.venv\Scripts\python.exe"
$fccVenvPython = "$FirmDir\.venv\Scripts\python.exe"
$fccServicesDir = "$InstallRoot\firm_command_center\services"
$winswExe = "$fccServicesDir\winsw.exe"

$pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonExe -or -not (Test-Path $pythonExe)) {
    if (Test-Path $venvPython) {
        $pythonExe = $venvPython
    } else {
        Write-Error "Python not found. Ensure .venv exists at $venvPython"
        if (-not $WhatIf) { exit 1 }
    }
}

$pwshPath = (Get-Command powershell -ErrorAction SilentlyContinue).Source
if (-not $pwshPath) { $pwshPath = "powershell.exe" }

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  ETA VPS Bootstrap â€” Full Stack Registration" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "Install root: $InstallRoot" -ForegroundColor Gray
Write-Host "Python:       $pythonExe" -ForegroundColor Gray
Write-Host ""

# â”€â”€ Venv validation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

Write-Host "=== Environment Validation ===" -ForegroundColor Green

if (Test-Path $venvPython) {
    Write-Host "  eta_engine\.venv:     PRESENT ($(& $venvPython --version 2>&1))" -ForegroundColor Green
} else {
    Write-Host "  eta_engine\.venv:     MISSING â€” run: uv sync --locked in $EtaEngineDir" -ForegroundColor Yellow
}

if (Test-Path $fccVenvPython) {
    Write-Host "  firm_command_center\.venv: PRESENT ($(& $fccVenvPython --version 2>&1))" -ForegroundColor Green
} else {
    Write-Host "  firm_command_center\.venv: MISSING â€” WinSW services will fail" -ForegroundColor Yellow
    Write-Host "    Create: cd $FirmDir && uv sync --locked" -ForegroundColor Gray
}

if (Test-Path $winswExe) {
    Write-Host "  winsw.exe:            PRESENT at $winswExe" -ForegroundColor Green
} else {
    Write-Host "  winsw.exe:            MISSING â€” WinSW services will fail" -ForegroundColor Yellow
}

# â”€â”€ Force Multiplier CLI checks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if (-not $SkipForceMultiplier) {
    Write-Host ""; Write-Host "=== Force Multiplier (Wave-19) ===" -ForegroundColor Green

    $hasNpx = Get-Command npx -ErrorAction SilentlyContinue
    if ($hasNpx) {
        Write-Host "  Claude CLI: npx available (Lead Architect)" -ForegroundColor Green
    } else {
        Write-Host "  Claude CLI: MISSING -- install: npm install -g @anthropic-ai/claude-code" -ForegroundColor Yellow
    }
    if ($hasNpx) {
        Write-Host "  Codex CLI:  npx available (Systems Expert)" -ForegroundColor Green
    } else {
        Write-Host "  Codex CLI:  MISSING -- install: npm install -g @openai/codex" -ForegroundColor Yellow
    }

    $codexCheck = (Get-Command codex -ErrorAction SilentlyContinue) -or (Get-Command npx -ErrorAction SilentlyContinue)
    if ($codexCheck) {
        Write-Host "  Codex CLI:            npx available (Systems Expert)" -ForegroundColor Green
    } else {
        Write-Host "  Codex CLI:            MISSING â€” install: npm install -g @openai/codex" -ForegroundColor Yellow
    }

    # Run Force Multiplier health probe if available
    $fmHealthScript = "$EtaEngineDir\scripts\force_multiplier_health.py"
    if (Test-Path $fmHealthScript) {
        Write-Host "  Running force_multiplier_health.py..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pythonExe $fmHealthScript 2>&1 | Select-Object -Last 5
        }
    }

    Write-Host "  NOTE: Run 'claude login' and 'codex login' on VPS if not yet authenticated" -ForegroundColor Gray
}

# â”€â”€ Verify secrets â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

Write-Host ""; Write-Host "=== Secrets ===" -ForegroundColor Green

$secretsDir = "$InstallRoot\secrets"
$fccSecretsDir = "$InstallRoot\firm_command_center\secrets"
$tokenFile = "$secretsDir\telegram_bot_token.txt"
$chatFile = "$secretsDir\telegram_chat_id.txt"
$quantumCreds = "$secretsDir\quantum_creds.json"

if (-not (Test-Path $secretsDir)) {
    New-Item -ItemType Directory -Force -Path $secretsDir | Out-Null
}

function Test-SecretPopulated($path, $name) {
    if (-not (Test-Path $path)) {
        Write-Host "  $name : MISSING â€” placeholder created" -ForegroundColor Yellow
        if (-not $WhatIf) {
            Set-Content -Path $path -Value "# Place your $name here" -Encoding UTF8
        }
        return $false
    }
    $content = Get-Content $path -Raw -ErrorAction SilentlyContinue
    if (-not $content -or $content -match "^#\s*Place your") {
        Write-Host "  $name : PLACEHOLDER â€” populate with real value" -ForegroundColor Yellow
        return $false
    }
    Write-Host "  $name : POPULATED" -ForegroundColor Green
    return $true
}

$secretsOk = $true
if (-not (Test-SecretPopulated $tokenFile "telegram_bot_token.txt")) { $secretsOk = $false }
if (-not (Test-SecretPopulated $chatFile "telegram_chat_id.txt")) { $secretsOk = $false }

if (-not (Test-Path $quantumCreds)) {
    $credsTemplate = '{"_comment":"Place D-Wave/IBM Quantum credentials here","dwave":{"token":"","solver":"Advantage_system4.1","region":"na-west-1"},"ibm":{"token":"","instance":"ibm-q/open/main","backend":"ibm_kyiv"},"budget":{"max_cost_per_job_usd":0.50,"max_cost_per_day_usd":5.00,"enable_cloud":false}}'
    Write-Host "  quantum_creds.json:  MISSING â€” template created" -ForegroundColor Yellow
    if (-not $WhatIf) {
        $credsTemplate | Out-File $quantumCreds -Encoding UTF8
    }
    $secretsOk = $false
} else {
    $qc = Get-Content $quantumCreds -Raw | ConvertFrom-Json
    if ($qc.enable_cloud -and -not $qc.dwave.token) {
        Write-Host "  quantum_creds.json:  cloud enabled but no D-Wave token" -ForegroundColor Yellow
        $secretsOk = $false
    } else {
        Write-Host "  quantum_creds.json:  PRESENT" -ForegroundColor Green
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

if (-not $secretsOk) {
    Write-Host "  WARNING: Some secrets are not populated. Services will run in degraded mode." -ForegroundColor Yellow
}

# â”€â”€ WinSW Service Registration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if (-not $SkipWinSW) {
    Write-Host ""; Write-Host "=== WinSW Windows Services ===" -ForegroundColor Green

    $services = @(
        @{Name="FirmCore";                    Xml="FirmCore.xml"},
        @{Name="FirmWatchdog";                Xml="FirmWatchdog.xml"},
        @{Name="FirmCommandCenter";           Xml="FirmCommandCenter.xml"},
        @{Name="FirmCommandCenterEdge";       Xml="FirmCommandCenterEdge.xml"},
        @{Name="FirmCommandCenterTunnel";     Xml="FirmCommandCenterTunnel.xml"},
        @{Name="ETAJarvisSupervisor";         Xml="ETAJarvisSupervisor.xml"}
    )

    if (Test-Path $winswExe) {
        foreach ($svc in $services) {
            $xmlPath = "$fccServicesDir\$($svc.Xml)"
            if (Test-Path $xmlPath) {
                $svcDir = "$fccServicesDir\$($svc.Name)"
                if (-not (Test-Path $svcDir)) { New-Item -ItemType Directory -Force -Path $svcDir | Out-Null }

                Copy-Item $xmlPath "$svcDir\$($svc.Xml)" -Force
                Copy-Item $winswExe "$svcDir\winsw.exe" -Force

                if (-not $WhatIf) {
                    & "$svcDir\winsw.exe" status 2>&1 | Out-Null
                    if ($LASTEXITCODE -ne 0) {
                        & "$svcDir\winsw.exe" install 2>&1 | Out-Null
                        Write-Host "  $($svc.Name): INSTALLED" -ForegroundColor Green
                    } else {
                        Write-Host "  $($svc.Name): ALREADY INSTALLED" -ForegroundColor Gray
                    }
                } else {
                    Write-Host "  $($svc.Name): WOULD INSTALL (WhatIf)" -ForegroundColor Gray
                }
            } else {
                Write-Host "  $($svc.Name): XML MISSING at $xmlPath" -ForegroundColor Yellow
            }
        }
    } else {
        Write-Host "  SKIPPED: winsw.exe not found at $winswExe" -ForegroundColor Yellow
        Write-Host "  WinSW services must be registered manually after placing winsw.exe" -ForegroundColor Gray
    }
}

# â”€â”€ Hermes Telegram Bridge â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if (-not $SkipHermes) {
    Write-Host ""; Write-Host "=== Hermes Telegram Bridge ===" -ForegroundColor Green
    $hermesScript = "$FirmDir\deploy\windows\install_hermes_service.ps1"
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

# â”€â”€ ETA Persona Tasks + Boot Tasks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if (-not $SkipETATasks) {
    Write-Host ""; Write-Host "=== ETA Persona Tasks ===" -ForegroundColor Green
    $registerTasksScript = "$EtaEngineDir\deploy\scripts\register_tasks.ps1"

    if (Test-Path $registerTasksScript) {
        Write-Host "  Registering 19 ETA persona tasks + 3 boot services..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $registerTasksScript -InstallDir $EtaEngineDir
        }
    } else {
        Write-Host "  register_tasks.ps1 not found at $registerTasksScript" -ForegroundColor Yellow
        Write-Host "  Run manually: $EtaEngineDir\deploy\scripts\register_tasks.ps1 -InstallDir $EtaEngineDir" -ForegroundColor Gray
    }

    Write-Host ""; Write-Host "=== ETA Dashboard API + Bridge Tasks ===" -ForegroundColor Green
    $dashboardApiTaskScript = "$EtaEngineDir\deploy\scripts\register_dashboard_api_task.ps1"
    $proxy8421TaskScript = "$EtaEngineDir\deploy\scripts\register_proxy8421_bridge_task.ps1"

    if (Test-Path $dashboardApiTaskScript) {
        Write-Host "  Registering canonical dashboard API task (127.0.0.1:8000)..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $dashboardApiTaskScript -Start
        }
    } else {
        Write-Host "  register_dashboard_api_task.ps1 not found at $dashboardApiTaskScript" -ForegroundColor Yellow
    }

    if (Test-Path $proxy8421TaskScript) {
        Write-Host "  Registering dashboard bridge task (127.0.0.1:8421 -> 8000)..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $proxy8421TaskScript -Root $EtaEngineDir -Start
        }
    } else {
        Write-Host "  register_proxy8421_bridge_task.ps1 not found at $proxy8421TaskScript" -ForegroundColor Yellow
    }
}

# â”€â”€ Health check scheduled task â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if (-not $SkipHealthCheck) {
    Write-Host ""; Write-Host "=== Health Check ===" -ForegroundColor Green
    $healthScript = "$EtaEngineDir\scripts\health_check.py"
    $healthOutDir = "$InstallRoot\firm_command_center\var\health"

    if (-not (Test-Path $healthOutDir)) {
        New-Item -ItemType Directory -Force -Path $healthOutDir | Out-Null
    }

    if (Test-Path $healthScript) {
        $taskName = "ETA-HealthCheck"

        # Run once now
        Write-Host "  Running health check..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pythonExe $healthScript --output-dir $healthOutDir 2>&1 | Select-Object -Last 5
        }

        # Register daily task
        $action = New-ScheduledTaskAction -Execute $pythonExe `
            -Argument "`"$healthScript`" --output-dir `"$healthOutDir`""
        $trigger = New-ScheduledTaskTrigger -Daily -At "08:00" -RepetitionInterval (New-TimeSpan -Hours 4) -RepetitionDuration (New-TimeSpan -Hours 24)
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

        if (-not $WhatIf) {
            Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
            Write-Host "  Registered: $taskName (every 4h)" -ForegroundColor Green
        }
    }
}

# â”€â”€ Quantum daily rebalance â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

# â”€â”€ DeepSeek + Codex scheduler ticks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
        Write-Host "  Register-DeepSeekScheduledTasks.ps1 not found â€” skipping" -ForegroundColor Yellow
    }
}

# --- Codex overnight operator + three-AI sync -------------------------------

if (-not $SkipCodexOperator) {
    Write-Host ""; Write-Host "=== Codex Overnight Operator ===" -ForegroundColor Green
    $codexOperatorScript = "$EtaEngineDir\deploy\scripts\register_codex_operator_task.ps1"

    if (Test-Path $codexOperatorScript) {
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $codexOperatorScript `
                -InstallDir $EtaEngineDir `
                -StateDir "$InstallRoot\var\eta_engine\state" `
                -LogDir "$InstallRoot\logs\eta_engine" `
                -PythonExe $pythonExe
        } else {
            Write-Host "  WOULD REGISTER: ETA-Codex-Overnight-Operator + ETA-ThreeAI-Sync" -ForegroundColor Gray
        }
    } else {
        Write-Host "  register_codex_operator_task.ps1 not found at $codexOperatorScript" -ForegroundColor Yellow
    }
}

# â”€â”€ IBKR Gateway Watchdog â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if (-not $SkipIbkrGateway) {
    Write-Host ""; Write-Host "=== IBKR Gateway Watchdog ===" -ForegroundColor Green
    $ibkrWatchdogScript = "$EtaEngineDir\deploy\windows\register_ibkr_gateway_watchdog_task.ps1"

    # Fall back to $FirmDir path if the canonical path doesn't exist
    if (-not (Test-Path $ibkrWatchdogScript)) {
        $ibkrWatchdogScript = "$FirmDir\deploy\windows\register_ibkr_gateway_watchdog_task.ps1"
    }

    if (Test-Path $ibkrWatchdogScript) {
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $ibkrWatchdogScript `
                -ApexRoot $FirmDir `
                -RunNow
            Write-Host "  Registered: ETAIbkrGatewayWatchdog (every 5m, auto-start on boot)" -ForegroundColor Green
        }
    } else {
        Write-Host "  register_ibkr_gateway_watchdog_task.ps1 not found â€” skipping" -ForegroundColor Yellow
    }
}

# â”€â”€ Final health check â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

Write-Host ""; Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  BOOTSTRAP COMPLETE" -ForegroundColor Green
Write-Host "====================================================" -ForegroundColor Cyan

if (-not $WhatIf) {
    Write-Host "Running final health check..." -ForegroundColor Gray
    $healthScript = "$EtaEngineDir\scripts\health_check.py"
    $healthOutDir = "$InstallRoot\firm_command_center\var\health"
    if (Test-Path $healthScript) {
        & $pythonExe $healthScript --output-dir $healthOutDir 2>&1 | Select-Object -Last 15
    }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        Write-Host "Health: GREEN" -ForegroundColor Green
    } elseif ($exitCode -eq 1) {
        Write-Host "Health: YELLOW â€” check action items above" -ForegroundColor Yellow
    } else {
        Write-Host "Health: RED â€” critical issues detected" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "Scheduled tasks:" -ForegroundColor White
Write-Host "  ETA-HealthCheck                 â€” every 4h" -ForegroundColor Gray
Write-Host "  ETA-Quantum-Daily-Rebalance      â€” daily 21:00 UTC" -ForegroundColor Gray
Write-Host "  ETA-Codex-Overnight-Operator     -- every 10m" -ForegroundColor Gray
Write-Host "  ETA-ThreeAI-Sync                 -- every 4h" -ForegroundColor Gray
Write-Host "  ETA-DeepSeek-MachineGate         â€” every 2h" -ForegroundColor Gray
Write-Host "  ETA-DeepSeek-CodexLane           â€” every 4h" -ForegroundColor Gray
Write-Host "  ETA-Hermes-Jarvis-Flush          â€” daily 00:30 UTC" -ForegroundColor Gray
Write-Host "  ETA-Executor      12 tasks  -- persona grunt work" -ForegroundColor Gray
Write-Host "  ETA-Steward        7 tasks  -- persona routine ops" -ForegroundColor Gray
Write-Host "  ETA-Reasoner       3 tasks  -- persona architectural" -ForegroundColor Gray
Write-Host "  ETA-Jarvis-Live                  â€” boot: logon trigger" -ForegroundColor Gray
Write-Host "  ETA-Avengers-Fleet               â€” boot: logon trigger" -ForegroundColor Gray
Write-Host "  ETA-Dashboard                    â€” boot: logon trigger" -ForegroundColor Gray
Write-Host "  ETA-IbkrGatewayWatchdog          â€” every 5m" -ForegroundColor Gray
Write-Host ""
Write-Host "WinSW Services:" -ForegroundColor White
Write-Host "  FirmCore                         â€” live runtime core" -ForegroundColor Gray
Write-Host "  FirmWatchdog                     â€” watchdog heartbeat" -ForegroundColor Gray
Write-Host "  FirmCommandCenter  -- dashboard on port 8420" -ForegroundColor Gray
Write-Host "  FirmCommandCenterEdge            â€” Caddy reverse proxy" -ForegroundColor Gray
Write-Host "  FirmCommandCenterTunnel          â€” Cloudflare tunnel" -ForegroundColor Gray
Write-Host "  HermesJarvisTelegram             â€” Telegram bridge" -ForegroundColor Gray
Write-Host "  ETAJarvisSupervisor              â€” strategy supervisor" -ForegroundColor Gray
Write-Host ""
Write-Host "Secrets needed:" -ForegroundColor White
Write-Host "  secrets/telegram_bot_token.txt    â€” for Hermes Telegram push" -ForegroundColor Gray
Write-Host "  secrets/telegram_chat_id.txt      â€” for Hermes Telegram push" -ForegroundColor Gray
Write-Host "  secrets/quantum_creds.json        â€” for cloud quantum (D-Wave/IBM)" -ForegroundColor Gray
Write-Host ""
Write-Host "Force Multiplier (Wave-19):" -ForegroundColor White
Write-Host "  Claude CLI: install + 'claude login' for Lead Architect tasks" -ForegroundColor Gray
Write-Host "  Codex CLI:  install + 'codex login' for Systems Expert tasks" -ForegroundColor Gray
Write-Host "  DeepSeek:   API key in .env for Worker Bee tasks (auto-fallback)" -ForegroundColor Gray
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Fill in secrets files at $secretsDir" -ForegroundColor Gray
Write-Host "  2. Authenticate CLIs: claude login && codex login" -ForegroundColor Gray
Write-Host "  3. Start services: Get-Service Firm* | Start-Service" -ForegroundColor Gray
Write-Host "  4. Verify tasks:   Get-ScheduledTask ETA-* | Select TaskName, State" -ForegroundColor Gray
Write-Host "  5. Monitor health: tail -f $InstallRoot\firm_command_center\var\health\current_health.json" -ForegroundColor Gray

