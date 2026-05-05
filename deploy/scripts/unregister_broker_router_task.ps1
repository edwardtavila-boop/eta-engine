# EVOLUTIONARY TRADING ALGO // unregister_broker_router_task.ps1
# =============================================================
# Counterpart to register_broker_router_task.ps1. Idempotent: if
# the task is absent, this is a no-op with a friendly message.
#
# Usage (run elevated):
#   powershell.exe -ExecutionPolicy Bypass -File `
#     C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\unregister_broker_router_task.ps1

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$TaskName = "ETA-Broker-Router"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "No '$TaskName' task found; nothing to do."
    exit 0
}

Write-Host "==> Stopping '$TaskName' (if running)..."
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

Write-Host "==> Unregistering '$TaskName'..."
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

Write-Host "OK: '$TaskName' removed."
