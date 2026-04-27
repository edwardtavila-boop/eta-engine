# Set persistent environment variables on the VPS.
#
# Run ONCE on the VPS as Administrator after the bootstrap. Reads a
# secrets file containing key=value lines (one per line) and writes
# them to the SYSTEM scope so every scheduled task picks them up.
#
# Usage on the VPS:
#
#     # Option 1: pipe the secrets in-line (most secure -- nothing
#     # touches disk)
#     "WEBHOOK_HMAC_SECRET=<paste-value-from-laptop-state-file>" | `
#         powershell -File deploy\scripts\set_vps_env_vars.ps1 -FromStdin
#
#     # Option 2: bring a secrets file via RDP file-transfer to a
#     # Windows protected dir and point at it
#     powershell -File deploy\scripts\set_vps_env_vars.ps1 `
#         -SecretsFile C:\eta_engine\.secrets\vps_env.txt
#
# Verifies that nothing logs the values; only key names are echoed.
[CmdletBinding()]
param(
    [string]$SecretsFile = "",
    [switch]$FromStdin,
    [string[]]$ExpectedKeys = @(
        "WEBHOOK_HMAC_SECRET",
        "ETA_VERDICT_WEBHOOK_URL",
        "RESEND_API_KEY",
        "PUSHOVER_USER",
        "PUSHOVER_TOKEN",
        "TASTYTRADE_USERNAME",
        "TASTYTRADE_PASSWORD",
        "IBKR_USERNAME",
        "ANTHROPIC_API_KEY"
    ),
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"

function Set-PersistentEnvVar([string]$key, [string]$value) {
    if ($DryRun) {
        Write-Host "  (DryRun) would set $key (length=$($value.Length))"
        return
    }
    try {
        [System.Environment]::SetEnvironmentVariable($key, $value, "Machine")
        Write-Host "  OK   $key (length=$($value.Length), system-scope)" -ForegroundColor Green
    } catch {
        # Fallback: User scope (no admin needed)
        try {
            [System.Environment]::SetEnvironmentVariable($key, $value, "User")
            Write-Host "  OK   $key (length=$($value.Length), user-scope -- not admin)" -ForegroundColor Yellow
        } catch {
            Write-Host "  FAIL $key : $($_.Exception.Message)" -ForegroundColor Red
        }
    }
}

# Source: stdin or file
$lines = @()
if ($FromStdin) {
    Write-Host "[set-vps-env] reading from stdin..."
    $stdin = [Console]::In.ReadToEnd()
    $lines = $stdin -split "`r?`n"
} elseif ($SecretsFile) {
    if (-not (Test-Path $SecretsFile)) {
        Write-Host "ERROR: secrets file not found: $SecretsFile" -ForegroundColor Red
        exit 1
    }
    Write-Host "[set-vps-env] reading from $SecretsFile"
    $lines = Get-Content $SecretsFile
} else {
    Write-Host "ERROR: must specify -SecretsFile <path> or -FromStdin" -ForegroundColor Red
    Write-Host ""
    Write-Host "Expected keys (any subset is OK):"
    foreach ($k in $ExpectedKeys) { Write-Host "  $k" }
    exit 1
}

$set = 0
$skipped = 0
foreach ($line in $lines) {
    $line = $line.Trim()
    if ([string]::IsNullOrEmpty($line) -or $line.StartsWith("#")) { continue }
    $parts = $line -split "=", 2
    if ($parts.Count -ne 2) {
        Write-Host "  SKIP malformed line"
        $skipped++
        continue
    }
    $key   = $parts[0].Trim()
    $value = $parts[1].Trim().Trim('"').Trim("'")
    if ([string]::IsNullOrEmpty($value)) {
        Write-Host "  SKIP $key (empty value)"
        $skipped++
        continue
    }
    Set-PersistentEnvVar -key $key -value $value
    $set++
}

Write-Host ""
Write-Host "[set-vps-env] set=$set skipped=$skipped"
Write-Host ""
Write-Host "RESTART scheduled tasks (or RDP session) so they pick up the new env vars:"
Write-Host "  Get-ScheduledTask -TaskName 'Eta-*','Apex-*' | Stop-ScheduledTask -ErrorAction SilentlyContinue"
Write-Host "  Get-ScheduledTask -TaskName 'Eta-*','Apex-*' | Start-ScheduledTask"
