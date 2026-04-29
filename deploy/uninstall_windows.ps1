# ============================================================================
# EVOLUTIONARY TRADING ALGO // uninstall_windows.ps1
# Removes all Apex-* Task Scheduler entries. Source code + .env preserved.
# Usage: powershell -ExecutionPolicy Bypass -File .\deploy\uninstall_windows.ps1
# ============================================================================
[CmdletBinding()]
param(
    [switch]$Purge,
    [string]$InstallDir = ""
)

if (-not $InstallDir) {
    $InstallDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$workspaceRoot = Split-Path -Parent $InstallDir

Write-Host "[apex-uninstall] stopping + removing Apex-* scheduled tasks" -ForegroundColor Cyan
Get-ScheduledTask -TaskName "Apex-*" -ErrorAction SilentlyContinue | ForEach-Object {
    try { Stop-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
    Write-Host "  removed $($_.TaskName)"
}

if ($Purge) {
    $stateDir = Join-Path $workspaceRoot "var\eta_engine\state"
    $logDir   = Join-Path $workspaceRoot "logs\eta_engine"
    Write-Host "[apex-uninstall] PURGE: removing state + logs" -ForegroundColor Yellow
    Remove-Item -Recurse -Force $stateDir -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $logDir -ErrorAction SilentlyContinue
}

Write-Host "[apex-uninstall] complete. Source + .env preserved." -ForegroundColor Green
