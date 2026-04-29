# eta_engine/deploy/scripts/run_dashboard_8421.ps1
# Primary ETA command center launcher (port 8421).
# This is now treated as the canonical operator surface.
#
# Usage:
#   .\eta_engine\deploy\scripts\run_dashboard_8421.ps1
#
# Then visit http://127.0.0.1:8421/ to test.

$ErrorActionPreference = "Stop"

$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$stateDir = Join-Path $workspaceRoot "var\eta_engine\state"
$logDir = Join-Path $workspaceRoot "logs\eta_engine"
New-Item -ItemType Directory -Force -Path $stateDir, $logDir | Out-Null
$env:ETA_STATE_DIR = $stateDir
$env:ETA_LOG_DIR = $logDir

# Ensure operator account exists (only on first run)
$users = Join-Path $stateDir "auth\users.json"
if (-not (Test-Path $users)) {
    Write-Host "First-run: creating operator account..."
    $username = Read-Host "Operator username"
    $pw = Read-Host "Operator password" -AsSecureString
    $pwPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
    python -c "from pathlib import Path; from eta_engine.deploy.scripts.dashboard_auth import create_user; create_user(Path(r'$users'), '$username', '$pwPlain')"
    Write-Host "Account created at $users"
}

# PIN for step-up
if (-not $env:ETA_DASHBOARD_STEP_UP_PIN) {
    Write-Host "Step-up PIN not set. Operator destructive actions (kill / flatten / V22 toggle) will be UNAVAILABLE." -ForegroundColor Yellow
    Write-Host "To enable: set `$env:ETA_DASHBOARD_STEP_UP_PIN = 'your-pin' before running this script." -ForegroundColor Yellow
    Write-Host ""
    $confirm = Read-Host "Continue without step-up PIN? (y/N)"
    if ($confirm -ne 'y' -and $confirm -ne 'Y') {
        exit 1
    }
}

Write-Host "Checking port 8421 availability..."
$existing = Get-NetTCPConnection -LocalPort 8421 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Port 8421 already in use. Stop conflicting service before launching dashboard." -ForegroundColor Red
    $existing | Select-Object -First 5 LocalAddress,LocalPort,OwningProcess,State | Format-Table
    exit 1
}

Write-Host "Starting ETA command center on http://127.0.0.1:8421/"
Write-Host "  State: $stateDir"
Write-Host "  Logs:  $logDir"
Write-Host "  Press Ctrl+C to stop."
Write-Host ""

python -m uvicorn eta_engine.deploy.scripts.dashboard_api:app `
    --host 127.0.0.1 --port 8421 --reload
