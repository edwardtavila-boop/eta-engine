# VPS Bootstrap -- one-command full stack startup
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

# -- Path resolution ---------------------------------------

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
Write-Host "  ETA VPS Bootstrap -- Full Stack Registration" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "Install root: $InstallRoot" -ForegroundColor Gray
Write-Host "Python:       $pythonExe" -ForegroundColor Gray
Write-Host ""

# -- Venv validation ---------------------------------------

Write-Host "=== Environment Validation ===" -ForegroundColor Green

if (Test-Path $venvPython) {
    Write-Host "  eta_engine\.venv:     PRESENT ($(& $venvPython --version 2>&1))" -ForegroundColor Green
} else {
    Write-Host "  eta_engine\.venv:     MISSING -- run: uv sync --locked in $EtaEngineDir" -ForegroundColor Yellow
}

if (Test-Path $fccVenvPython) {
    Write-Host "  firm_command_center\.venv: PRESENT ($(& $fccVenvPython --version 2>&1))" -ForegroundColor Green
} else {
    Write-Host "  firm_command_center\.venv: MISSING -- WinSW services will fail" -ForegroundColor Yellow
    Write-Host "    Create: cd $FirmDir && uv sync --locked" -ForegroundColor Gray
}

if (Test-Path $winswExe) {
    Write-Host "  winsw.exe:            PRESENT at $winswExe" -ForegroundColor Green
} else {
    Write-Host "  winsw.exe:            MISSING -- WinSW services will fail" -ForegroundColor Yellow
}

# -- Force Multiplier CLI checks ---------------------------

if (-not $SkipForceMultiplier) {
    Write-Host ""; Write-Host "=== Force Multiplier (Wave-19) ===" -ForegroundColor Green

    $hasNpx = Get-Command npx -ErrorAction SilentlyContinue
    if ($hasNpx) {
        Write-Host "  Codex CLI:  available (Lead Architect + Systems Expert)" -ForegroundColor Green
    } else {
        Write-Host "  Codex CLI:  MISSING -- install: npm install -g @openai/codex" -ForegroundColor Yellow
    }

    $codexCheck = (Get-Command codex -ErrorAction SilentlyContinue) -or (Get-Command npx -ErrorAction SilentlyContinue)
    if ($codexCheck) {
        Write-Host "  Codex CLI:            available (Lead Architect + Systems Expert)" -ForegroundColor Green
    } else {
        Write-Host "  Codex CLI:            MISSING -- install: npm install -g @openai/codex" -ForegroundColor Yellow
    }

    # Run Force Multiplier health probe if available
    $fmHealthScript = "$EtaEngineDir\scripts\force_multiplier_health.py"
    if (Test-Path $fmHealthScript) {
        Write-Host "  Running force_multiplier_health.py..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pythonExe $fmHealthScript 2>&1 | Select-Object -Last 5
        }
    }

    Write-Host "  NOTE: Run 'codex login' on VPS if not yet authenticated; Claude is disabled" -ForegroundColor Gray
}

# -- Verify secrets ----------------------------------------

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
        Write-Host "  $name : MISSING -- placeholder created" -ForegroundColor Yellow
        if (-not $WhatIf) {
            Set-Content -Path $path -Value "# Place your $name here" -Encoding UTF8
        }
        return $false
    }
    $content = Get-Content $path -Raw -ErrorAction SilentlyContinue
    if (-not $content -or $content -match "^#\s*Place your") {
        Write-Host "  $name : PLACEHOLDER -- populate with real value" -ForegroundColor Yellow
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
    Write-Host "  quantum_creds.json:  MISSING -- template created" -ForegroundColor Yellow
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

# -- WinSW Service Registration ---------------------------

if (-not $SkipWinSW) {
    Write-Host ""; Write-Host "=== WinSW Windows Services ===" -ForegroundColor Green

    $services = @(
        @{Name="FirmCore";                    Xml="FirmCore.xml";                    XmlPath="$fccServicesDir\FirmCore.xml"},
        @{Name="FirmWatchdog";                Xml="FirmWatchdog.xml";                XmlPath="$fccServicesDir\FirmWatchdog.xml"},
        @{Name="FirmCommandCenter";           Xml="FirmCommandCenter.xml";           XmlPath="$EtaEngineDir\deploy\FirmCommandCenter_canonical.xml"},
        @{Name="FirmCommandCenterEdge";       Xml="FirmCommandCenterEdge.xml";       XmlPath="$fccServicesDir\FirmCommandCenterEdge.xml"},
        @{Name="FirmCommandCenterTunnel";     Xml="FirmCommandCenterTunnel.xml";     XmlPath="$fccServicesDir\FirmCommandCenterTunnel.xml"},
        @{Name="HermesJarvisTelegram";        Xml="HermesJarvisTelegram.xml";        XmlPath="$fccServicesDir\HermesJarvisTelegram.xml"},
        @{Name="ETAJarvisSupervisor";         Xml="ETAJarvisSupervisor.xml";         XmlPath="$fccServicesDir\ETAJarvisSupervisor.xml"},
        @{Name="FmStatusServer";              Xml="FmStatusServer.xml";              XmlPath="$EtaEngineDir\deploy\FmStatusServer.xml"}
    )

    if (Test-Path $winswExe) {
        foreach ($svc in $services) {
            $xmlPath = $svc.XmlPath
            if (Test-Path $xmlPath) {
                $svcDir = "$fccServicesDir\$($svc.Name)"
                if (-not (Test-Path $svcDir)) { New-Item -ItemType Directory -Force -Path $svcDir | Out-Null }

                Copy-Item $xmlPath "$svcDir\$($svc.Xml)" -Force
                $serviceExe = "$svcDir\$($svc.Name).exe"
                Copy-Item $winswExe $serviceExe -Force

                if (-not $WhatIf) {
                    & $serviceExe status 2>&1 | Out-Null
                    if ($LASTEXITCODE -ne 0) {
                        & $serviceExe install 2>&1 | Out-Null
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

# -- Hermes Telegram Bridge -------------------------------

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

# -- ETA Persona Tasks + Boot Tasks ----------------------

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
    $dashboardProxyWatchdogTaskScript = "$EtaEngineDir\deploy\scripts\register_dashboard_proxy_watchdog_task.ps1"
    $paperLiveTransitionTaskScript = "$EtaEngineDir\deploy\scripts\register_paper_live_transition_check_task.ps1"
    $etaReadinessSnapshotTaskScript = "$EtaEngineDir\deploy\scripts\register_eta_readiness_snapshot_task.ps1"
    $vpsOpsHardeningTaskScript = "$EtaEngineDir\deploy\scripts\register_vps_ops_hardening_audit_task.ps1"
    $symbolIntelCollectorTaskScript = "$EtaEngineDir\deploy\scripts\register_symbol_intelligence_collector_task.ps1"
    $operatorQueueHeartbeatTaskScript = "$EtaEngineDir\deploy\scripts\register_operator_queue_heartbeat_task.ps1"
    $cloudflareTunnelTaskScript = "$EtaEngineDir\deploy\scripts\register_cloudflare_named_tunnel_task.ps1"
    $etaWatchdogTaskScript = "$EtaEngineDir\deploy\scripts\register_eta_watchdog_task.ps1"

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

    if (Test-Path $dashboardProxyWatchdogTaskScript) {
        Write-Host "  Registering dashboard proxy watchdog task (repairs 127.0.0.1:8421)..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $dashboardProxyWatchdogTaskScript -Start -RestartExistingProcess
        }
    } else {
        Write-Host "  register_dashboard_proxy_watchdog_task.ps1 not found at $dashboardProxyWatchdogTaskScript" -ForegroundColor Yellow
    }

    if (Test-Path $cloudflareTunnelTaskScript) {
        Write-Host "  Registering named Cloudflare tunnel task..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $cloudflareTunnelTaskScript -Start -RestartExistingProcess
        }
    } else {
        Write-Host "  register_cloudflare_named_tunnel_task.ps1 not found at $cloudflareTunnelTaskScript" -ForegroundColor Yellow
    }

    if (Test-Path $paperLiveTransitionTaskScript) {
        Write-Host "  Registering paper-live transition cache refresher task (every 5m)..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $paperLiveTransitionTaskScript -Start
        }
    } else {
        Write-Host "  register_paper_live_transition_check_task.ps1 not found at $paperLiveTransitionTaskScript" -ForegroundColor Yellow
    }

    if (Test-Path $etaReadinessSnapshotTaskScript) {
        Write-Host "  Registering ETA readiness snapshot refresher task (every 5m, read-only)..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $etaReadinessSnapshotTaskScript -Start
        }
    } else {
        Write-Host "  register_eta_readiness_snapshot_task.ps1 not found at $etaReadinessSnapshotTaskScript" -ForegroundColor Yellow
    }

    if (Test-Path $vpsOpsHardeningTaskScript) {
        Write-Host "  Registering VPS ops hardening audit task (every 5m, read-only)..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $vpsOpsHardeningTaskScript -Start
        }
    } else {
        Write-Host "  register_vps_ops_hardening_audit_task.ps1 not found at $vpsOpsHardeningTaskScript" -ForegroundColor Yellow
    }

    if (Test-Path $symbolIntelCollectorTaskScript) {
        Write-Host "  Registering symbol intelligence collector task (every 5m)..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $symbolIntelCollectorTaskScript -Start
        }
    } else {
        Write-Host "  register_symbol_intelligence_collector_task.ps1 not found at $symbolIntelCollectorTaskScript" -ForegroundColor Yellow
    }

    if (Test-Path $operatorQueueHeartbeatTaskScript) {
        Write-Host "  Registering operator queue heartbeat task (every 5m, read-only)..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $operatorQueueHeartbeatTaskScript -Start
        }
    } else {
        Write-Host "  register_operator_queue_heartbeat_task.ps1 not found at $operatorQueueHeartbeatTaskScript" -ForegroundColor Yellow
    }

    if (Test-Path $etaWatchdogTaskScript) {
        Write-Host "  Registering long-running ETA watchdog task..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $etaWatchdogTaskScript `
                -Root $InstallRoot `
                -PythonExe $pythonExe `
                -Start `
                -RestartExistingProcess
        }
    } else {
        Write-Host "  register_eta_watchdog_task.ps1 not found at $etaWatchdogTaskScript" -ForegroundColor Yellow
    }
}

# -- Health check scheduled task ---------------------------

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

# -- Quantum daily rebalance -------------------------------

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

# -- DeepSeek + Codex scheduler ticks ---------------------

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
        Write-Host "  Register-DeepSeekScheduledTasks.ps1 not found -- skipping" -ForegroundColor Yellow
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

# -- IBKR Gateway Watchdog ------------------------------

if (-not $SkipIbkrGateway) {
    Write-Host ""; Write-Host "=== IBKR Gateway Recovery ===" -ForegroundColor Green
    $gatewayAuthorityScript = "$EtaEngineDir\deploy\scripts\set_gateway_authority.ps1"
    $twsWatchdogScript = "$EtaEngineDir\deploy\scripts\register_tws_watchdog_task.ps1"
    $ibkrRecoveryScript = "$EtaEngineDir\deploy\scripts\register_ibgateway_reauth_task.ps1"
    $gatewayAuthorityReady = $false

    if (Test-Path $gatewayAuthorityScript) {
        if (-not $WhatIf) {
            $authorityOutput = & $pwshPath -ExecutionPolicy Bypass -File $gatewayAuthorityScript -Apply -Role vps 2>&1
            if ($LASTEXITCODE -eq 0) {
                $gatewayAuthorityReady = $true
                Write-Host "  Marked: this VPS is the IBKR Gateway authority host" -ForegroundColor Green
            } else {
                Write-Host "  Skipping IBKR Gateway task registration: this host was not accepted as Gateway authority" -ForegroundColor Yellow
                if ($authorityOutput) {
                    Write-Host "    $authorityOutput" -ForegroundColor DarkYellow
                }
            }
        } else {
            $gatewayAuthorityReady = $true
            Write-Host "  WOULD MARK: this VPS as the IBKR Gateway authority host" -ForegroundColor Gray
        }
    } else {
        Write-Host "  set_gateway_authority.ps1 not found - Gateway launch guard will remain unmarked" -ForegroundColor Yellow
    }

    if ($gatewayAuthorityReady) {
        if (Test-Path $twsWatchdogScript) {
            if (-not $WhatIf) {
                & $pwshPath -ExecutionPolicy Bypass -File $twsWatchdogScript -Start
                Write-Host "  Registered: ETA-TWS-Watchdog (startup + every 60s, release-guard freshness)" -ForegroundColor Green
            }
        } else {
            Write-Host "  register_tws_watchdog_task.ps1 not found - skipping" -ForegroundColor Yellow
        }

        if (Test-Path $ibkrRecoveryScript) {
            if (-not $WhatIf) {
                & $pwshPath -ExecutionPolicy Bypass -File $ibkrRecoveryScript -Start
                Write-Host "  Registered: ETA-IBGateway-Reauth (startup + every 5m, canonical recovery lane)" -ForegroundColor Green
            }
        } else {
            Write-Host "  register_ibgateway_reauth_task.ps1 not found - skipping" -ForegroundColor Yellow
        }
    }
}

# -- Final health check -----------------------------------

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
        Write-Host "Health: YELLOW -- check action items above" -ForegroundColor Yellow
    } else {
        Write-Host "Health: RED -- critical issues detected" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "Scheduled tasks:" -ForegroundColor White
Write-Host "  ETA-HealthCheck                 -- every 4h" -ForegroundColor Gray
Write-Host "  ETA-Quantum-Daily-Rebalance      -- daily 21:00 UTC" -ForegroundColor Gray
Write-Host "  ETA-Codex-Overnight-Operator     -- every 10m" -ForegroundColor Gray
Write-Host "  ETA-ThreeAI-Sync                 -- every 4h" -ForegroundColor Gray
Write-Host "  ETA-DeepSeek-MachineGate         -- every 2h" -ForegroundColor Gray
Write-Host "  ETA-DeepSeek-CodexLane           -- every 4h" -ForegroundColor Gray
Write-Host "  ETA-Hermes-Jarvis-Flush          -- daily 00:30 UTC" -ForegroundColor Gray
Write-Host "  ETA-Executor      12 tasks  -- persona grunt work" -ForegroundColor Gray
Write-Host "  ETA-Steward        7 tasks  -- persona routine ops" -ForegroundColor Gray
Write-Host "  ETA-Reasoner       3 tasks  -- persona architectural" -ForegroundColor Gray
Write-Host "  ETA-Jarvis-Live                  -- boot: logon trigger" -ForegroundColor Gray
Write-Host "  ETA-Avengers-Fleet               -- boot: logon trigger" -ForegroundColor Gray
Write-Host "  ETA-Dashboard                    -- boot: logon trigger" -ForegroundColor Gray
Write-Host "  ETA-Dashboard-API                -- 127.0.0.1:8000 canonical API" -ForegroundColor Gray
Write-Host "  ETA-Proxy-8421                   -- ops bridge 127.0.0.1:8421 -> 8000" -ForegroundColor Gray
Write-Host "  ETA-Cloudflare-Tunnel            -- boot/logon named tunnel" -ForegroundColor Gray
Write-Host "  ETA-Dashboard-Proxy-Watchdog     -- boot/logon 8421 self-heal" -ForegroundColor Gray
Write-Host "  ETA-PaperLiveTransitionCheck     -- boot/logon + every 5m" -ForegroundColor Gray
Write-Host "  ETA-VpsOpsHardeningAudit         -- boot/logon + every 5m read-only audit" -ForegroundColor Gray
Write-Host "  ETA-SymbolIntelCollector         -- boot/logon + every 5m data lake refresh" -ForegroundColor Gray
Write-Host "  ETA-OperatorQueueHeartbeat       -- boot/logon + every 5m read-only queue snapshot" -ForegroundColor Gray
Write-Host "  ETA-Watchdog                     -- boot/logon runtime watchdog" -ForegroundColor Gray
Write-Host "  ETA-TWS-Watchdog                 -- startup + every 60s TWS health" -ForegroundColor Gray
Write-Host "  ETA-IBGateway-Reauth             -- startup + every 5m" -ForegroundColor Gray
Write-Host ""
Write-Host "WinSW Services:" -ForegroundColor White
Write-Host "  FirmCore                         -- live runtime core" -ForegroundColor Gray
Write-Host "  FirmWatchdog                     -- watchdog heartbeat" -ForegroundColor Gray
Write-Host "  FirmCommandCenter                -- legacy service; ETA API is 8000 + proxy 8421" -ForegroundColor Gray
Write-Host "  FirmCommandCenterEdge            -- Caddy reverse proxy" -ForegroundColor Gray
Write-Host "  FirmCommandCenterTunnel          -- Cloudflare tunnel" -ForegroundColor Gray
Write-Host "  HermesJarvisTelegram             -- Telegram bridge" -ForegroundColor Gray
Write-Host "  ETAJarvisSupervisor              -- strategy supervisor" -ForegroundColor Gray
Write-Host "  FmStatusServer                   -- Force Multiplier status on 127.0.0.1:8422" -ForegroundColor Gray
Write-Host ""
Write-Host "Secrets needed:" -ForegroundColor White
Write-Host "  secrets/telegram_bot_token.txt    -- for Hermes Telegram push" -ForegroundColor Gray
Write-Host "  secrets/telegram_chat_id.txt      -- for Hermes Telegram push" -ForegroundColor Gray
Write-Host "  secrets/quantum_creds.json        -- for cloud quantum (D-Wave/IBM)" -ForegroundColor Gray
Write-Host ""
Write-Host "Force Multiplier (Wave-19):" -ForegroundColor White
Write-Host "  Codex CLI:  install + 'codex login' for Lead Architect + Systems Expert tasks" -ForegroundColor Gray
Write-Host "  DeepSeek:   API key in .env for Worker Bee tasks (auto-fallback)" -ForegroundColor Gray
Write-Host "  Claude:     disabled legacy lane; do not seed Anthropic keys" -ForegroundColor Gray
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Fill in secrets files at $secretsDir" -ForegroundColor Gray
Write-Host "  2. Authenticate Codex CLI: codex login" -ForegroundColor Gray
Write-Host "  3. Start services: Get-Service Firm* | Start-Service" -ForegroundColor Gray
Write-Host "  4. Verify tasks:   Get-ScheduledTask ETA-* | Select TaskName, State" -ForegroundColor Gray
Write-Host "  5. Monitor health: tail -f $InstallRoot\firm_command_center\var\health\current_health.json" -ForegroundColor Gray


