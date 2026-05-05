# Wave-19 Deploy — one-shot deployment to VPS
# ============================================
# Pushes all Wave-19 changes (Force Multiplier, Fleet integration, VPS bootstrap v2,
# security fixes, dashboard improvements) to the VPS in a single command.
#
# Usage from VPS host (as Administrator):
#   cd C:\EvolutionaryTradingAlgo
#   pwsh -ExecutionPolicy Bypass -File .\eta_engine\deploy\wave19_deploy.ps1
#
# What this does:
#   1. Git pull all submodules (eta_engine, firm, mnq_backtest)
#   2. Install CLIs: npm install -g @anthropic-ai/claude-code @openai/codex
#   3. Run vps_bootstrap.ps1 (WinSW services, scheduled tasks, Force Multiplier checks)
#   4. Sync env vars: python eta_engine/scripts/env_sync.py --apply
#   5. Validate: health_check.py + secrets_validator.py + force_multiplier_health.py
#   6. Start all services
#   7. Run shadow fleet validation
#   8. Print final status

param(
    [string]$InstallRoot = "C:\EvolutionaryTradingAlgo",
    [switch]$SkipGitPull,
    [switch]$SkipCLIInstall,
    [switch]$SkipBootstrap,
    [switch]$SkipValidation,
    [switch]$SkipServices,
    [switch]$SkipShadowFleet,
    [switch]$WhatIf
)

$ErrorActionPreference = "Continue"
$pwshPath = (Get-Command powershell -ErrorAction SilentlyContinue).Source
if (-not $pwshPath) { $pwshPath = "powershell.exe" }
$EtaEngineDir = "$InstallRoot\eta_engine"
$pythonExe = "$EtaEngineDir\.venv\Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $pythonExe) {
    Write-Error "Python not found. Run: uv sync --locked in eta_engine/"
    exit 1
}

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  WAVE-19 DEPLOY — Force Multiplier + Fleet + Dashboard" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "Install root: $InstallRoot" -ForegroundColor Gray
Write-Host "Python:       $pythonExe" -ForegroundColor Gray
Write-Host ""

# ── Step 1: Git pull ────────────────────────────────────

if (-not $SkipGitPull) {
    Write-Host "=== Step 1/7: Git Pull ===" -ForegroundColor Green
    Push-Location $InstallRoot
    Write-Host "  Pulling root..." -ForegroundColor Gray
    git pull 2>&1 | Select-Object -Last 3

    foreach ($sub in @("eta_engine", "firm", "mnq_backtest")) {
        Push-Location "$InstallRoot\$sub"
        Write-Host "  Pulling $sub..." -ForegroundColor Gray
        git pull 2>&1 | Select-Object -Last 2
        Pop-Location
    }
    Pop-Location
}

# ── Step 2: Install CLIs ────────────────────────────────

if (-not $SkipCLIInstall) {
    Write-Host ""; Write-Host "=== Step 2/7: Install Force Multiplier CLIs ===" -ForegroundColor Green
    Write-Host "  Installing Claude CLI (Lead Architect)..." -ForegroundColor Gray
    npm install -g @anthropic-ai/claude-code 2>&1 | Select-Object -Last 2
    Write-Host "  Installing Codex CLI (Systems Expert)..." -ForegroundColor Gray
    npm install -g @openai/codex 2>&1 | Select-Object -Last 2

    Write-Host ""
    Write-Host "  ACTION REQUIRED:" -ForegroundColor Yellow
    Write-Host "    Run: claude login" -ForegroundColor Yellow
    Write-Host "    Run: codex login" -ForegroundColor Yellow
    Write-Host "  (Open browser windows will appear for OAuth)" -ForegroundColor Gray
    Write-Host ""
    Write-Host "  Press ENTER after completing both logins..." -ForegroundColor Cyan
    if (-not $WhatIf) { Read-Host }
}

# ── Step 3: VPS Bootstrap ───────────────────────────────

if (-not $SkipBootstrap) {
    Write-Host ""; Write-Host "=== Step 3/7: VPS Bootstrap (v2) ===" -ForegroundColor Green
    $bootstrapScript = "$EtaEngineDir\deploy\vps_bootstrap.ps1"
    if (Test-Path $bootstrapScript) {
        Write-Host "  Running vps_bootstrap.ps1..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pwshPath -ExecutionPolicy Bypass -File $bootstrapScript
        }
    } else {
        Write-Host "  vps_bootstrap.ps1 not found — checking for v2 fallback..." -ForegroundColor Yellow
        $v2Script = "$EtaEngineDir\deploy\vps_bootstrap_v2.ps1"
        if (Test-Path $v2Script) {
            & $pwshPath -ExecutionPolicy Bypass -File $v2Script
        }
    }
}

# ── Step 4: Sync env ────────────────────────────────────

Write-Host ""; Write-Host "=== Step 4/7: Sync Environment Variables ===" -ForegroundColor Green
$envSyncScript = "$EtaEngineDir\scripts\env_sync.py"
if (Test-Path $envSyncScript) {
    if (-not $WhatIf) {
        & $pythonExe $envSyncScript --apply 2>&1
    }
    Write-Host "  Env sync complete" -ForegroundColor Green
} else {
    Write-Host "  env_sync.py not found — skipping" -ForegroundColor Yellow
}

# ── Step 5: Validate ────────────────────────────────────

if (-not $SkipValidation) {
    Write-Host ""; Write-Host "=== Step 5/7: Validation ===" -ForegroundColor Green

    Write-Host "  Running health_check.py..." -ForegroundColor Gray
    if (-not $WhatIf) {
        & $pythonExe "$EtaEngineDir\scripts\health_check.py" 2>&1 | Select-Object -Last 5
    }
    $healthExit = $LASTEXITCODE

    Write-Host "  Running secrets_validator.py..." -ForegroundColor Gray
    if (-not $WhatIf) {
        & $pythonExe "$EtaEngineDir\scripts\secrets_validator.py" 2>&1 | Select-Object -Last 5
    }
    $secretsExit = $LASTEXITCODE

    Write-Host "  Running force_multiplier_health.py..." -ForegroundColor Gray
    if (-not $WhatIf) {
        & $pythonExe "$EtaEngineDir\scripts\force_multiplier_health.py" --live 2>&1 | Select-Object -Last 8
    }
    $fmExit = $LASTEXITCODE

    Write-Host ""
    if ($healthExit -eq 0 -and $secretsExit -eq 0 -and $fmExit -eq 0) {
        Write-Host "  ALL VALIDATIONS PASSED" -ForegroundColor Green
    } else {
        Write-Host "  Some validations reported issues (see above)" -ForegroundColor Yellow
    }
}

# ── Step 6: Start services ──────────────────────────────

if (-not $SkipServices) {
    Write-Host ""; Write-Host "=== Step 6/7: Start Services ===" -ForegroundColor Green

    $services = @(
        "FirmCore",
        "FirmWatchdog",
        "FirmCommandCenter",
        "HermesJarvisTelegram",
        "ETAJarvisSupervisor"
    )

    foreach ($svc in $services) {
        $status = (Get-Service $svc -ErrorAction SilentlyContinue).Status
        if ($status -eq "Running") {
            Write-Host "  $svc : ALREADY RUNNING" -ForegroundColor Gray
        } elseif ($status) {
            if (-not $WhatIf) { Start-Service $svc -ErrorAction SilentlyContinue }
            Write-Host "  $svc : STARTED" -ForegroundColor Green
        } else {
            Write-Host "  $svc : NOT INSTALLED" -ForegroundColor Yellow
        }
    }

    # Edge/Tunnel services (optional, start if present)
    foreach ($svc in @("FirmCommandCenterEdge", "FirmCommandCenterTunnel")) {
        $status = (Get-Service $svc -ErrorAction SilentlyContinue).Status
        if ($status) {
            if (-not $WhatIf -and $status -ne "Running") { Start-Service $svc -ErrorAction SilentlyContinue }
            Write-Host "  $svc : $(if($status -eq 'Running'){'ALREADY RUNNING'}else{'STARTED'})" -ForegroundColor Gray
        }
    }
}

# ── Step 7: Shadow fleet validation ─────────────────────

if (-not $SkipShadowFleet) {
    Write-Host ""; Write-Host "=== Step 7/7: Shadow Fleet Validation ===" -ForegroundColor Green
    $shadowScript = "$EtaEngineDir\scripts\shadow_fleet_validator.py"
    if (Test-Path $shadowScript) {
        Write-Host "  Running shadow fleet comparison (36 tasks across 6 categories)..." -ForegroundColor Gray
        if (-not $WhatIf) {
            & $pythonExe $shadowScript --tasks 6 --output var/shadow_validation.jsonl 2>&1 | Select-Object -Last 20
        }
    } else {
        Write-Host "  shadow_fleet_validator.py not found — skipping" -ForegroundColor Yellow
    }
}

# ── Final status ────────────────────────────────────────

Write-Host ""; Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  WAVE-19 DEPLOY COMPLETE" -ForegroundColor Green
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Verification checklist:" -ForegroundColor White
Write-Host "  [ ] All 7 WinSW services running: Get-Service Firm*,Hermes*,ETA* | Select Name,Status" -ForegroundColor Gray
Write-Host "  [ ] Scheduled tasks: Get-ScheduledTask ETA-* | Select TaskName,State" -ForegroundColor Gray
Write-Host "  [ ] Force Multiplier: curl http://127.0.0.1:8420/api/fm/status" -ForegroundColor Gray
Write-Host "  [ ] Shadow results: cat var/shadow_validation.jsonl | python -m json.tool" -ForegroundColor Gray
Write-Host "  [ ] Telegram: send /jarvis STATUS to verify Hermes bridge" -ForegroundColor Gray
Write-Host ""
Write-Host "Paper soak: let it run 7 days before flipping ETA_MODE=LIVE" -ForegroundColor Yellow
