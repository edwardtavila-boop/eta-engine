# eta_engine/deploy/scripts/run_dashboard_8421.ps1
# Stage 0 of the dashboard rebuild rollout.
# Runs the new dashboard on port 8421 ALONGSIDE the existing one on 8420
# so the operator can QA before cutover.
#
# Usage:
#   .\eta_engine\deploy\scripts\run_dashboard_8421.ps1
#
# Then visit http://127.0.0.1:8421/ to test.

$ErrorActionPreference = "Stop"

# Ensure operator account exists (only on first run)
$users = Join-Path $env:LOCALAPPDATA "eta_engine\state\auth\users.json"
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

Write-Host "Starting Stage 0 dashboard on http://127.0.0.1:8421/"
Write-Host "  (existing 8420 dashboard is unaffected; this runs in parallel)"
Write-Host "  Press Ctrl+C to stop."
Write-Host ""

python -m uvicorn eta_engine.deploy.scripts.dashboard_api:app `
    --host 127.0.0.1 --port 8421 --reload
