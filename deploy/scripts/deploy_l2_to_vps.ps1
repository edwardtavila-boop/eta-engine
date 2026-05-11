# EVOLUTIONARY TRADING ALGO // deploy/scripts/deploy_l2_to_vps.ps1
# ================================================================
# One-shot VPS deployment for the L2 supercharge stack.
#
# Run this on the operator workstation AFTER pushing eta_engine
# commits to origin and bumping the superproject gitlink.  The script:
#
#   1. SSH to forex-vps
#   2. Pull main + sync eta_engine submodule to current SHA
#   3. Run register_l2_cron_tasks.ps1 -StartNow on the VPS
#   4. Verify all ETA-L2-* tasks land in Ready/Running state
#
# Usage:
#   .\eta_engine\deploy\scripts\deploy_l2_to_vps.ps1
#   .\eta_engine\deploy\scripts\deploy_l2_to_vps.ps1 -SkipPull
#   .\eta_engine\deploy\scripts\deploy_l2_to_vps.ps1 -SshHost forex-vps
#
# Prerequisites (one-time):
#   - SSH alias `forex-vps` resolves and accepts pubkey auth
#   - VPS has C:\EvolutionaryTradingAlgo cloned with submodules
#   - VPS has Python 3.12+ at the default path or ETA_PYTHON_EXE set
#
# Idempotent: re-running re-registers every task without errors.

param(
    [string]$SshHost = "forex-vps",
    [switch]$SkipPull,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

Write-Host "=== L2 supercharge VPS deployment ==="
Write-Host "host        : $SshHost"
Write-Host "skip pull   : $SkipPull"
Write-Host "dry run     : $DryRun"
Write-Host ""

if ($DryRun) {
    Write-Host "[DRY RUN] would execute:"
    Write-Host "  ssh $SshHost 'cd C:\EvolutionaryTradingAlgo; git pull --recurse-submodules'"
    Write-Host "  ssh $SshHost 'pwsh C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\register_l2_cron_tasks.ps1 -StartNow'"
    Write-Host "  ssh $SshHost 'Get-ScheduledTask -TaskName ETA-L2-* | Format-Table -AutoSize'"
    exit 0
}

# 1. Pull latest on VPS
if (-not $SkipPull) {
    Write-Host "[1/3] Pulling latest on VPS..."
    $pullCmd = 'cd C:\EvolutionaryTradingAlgo; git pull --recurse-submodules; git submodule update --init --recursive'
    & ssh $SshHost "powershell -NoProfile -Command `"$pullCmd`""
    if ($LASTEXITCODE -ne 0) {
        Write-Error "VPS pull failed with exit $LASTEXITCODE"
        exit 1
    }
    Write-Host ""
}

# 2. Register cron tasks
Write-Host "[2/3] Registering cron tasks..."
$registerCmd = 'C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\register_l2_cron_tasks.ps1 -StartNow'
& ssh $SshHost "powershell -NoProfile -ExecutionPolicy Bypass -File `"$registerCmd`""
if ($LASTEXITCODE -ne 0) {
    Write-Error "Cron registration failed with exit $LASTEXITCODE"
    exit 1
}
Write-Host ""

# 3. Verify
Write-Host "[3/3] Verifying tasks..."
$verifyCmd = "Get-ScheduledTask -TaskName 'ETA-L2-*' | Select-Object TaskName, State | Format-Table -AutoSize"
& ssh $SshHost "powershell -NoProfile -Command `"$verifyCmd`""

Write-Host ""
Write-Host "=== Deployment complete ==="
Write-Host "Expected: 6 daily + 8 weekly = 14 tasks in Ready state"
Write-Host "Sweep summary: ssh $SshHost 'powershell -Command Get-ScheduledTask -TaskName ETA-L2-* | Measure-Object | Select Count'"
