$ErrorActionPreference = "Stop"
$pwFile = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibkr_pw.txt"
$user = "trader"

Write-Host "--- Creating fallback IBC password file on VPS ---"
Write-Host "  path : $pwFile"
Write-Host "  user : $user"
Write-Host ""

# 1. Ensure parent dir exists
$parent = Split-Path -Parent $pwFile
if (-not (Test-Path $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Write-Host "  created parent dir: $parent"
}

# 2. Create the file (empty until operator writes the password)
if (Test-Path $pwFile) {
    Write-Host "  pw file already exists — leaving content alone"
} else {
    # Write a sentinel comment so the file is non-empty but won't authenticate
    # if -IbcPasswordFile is wired up before operator overwrites it. Reading
    # First-Line of the file would return this sentinel; IBC would reject it
    # and the next restart would surface a clear error rather than silently
    # logging in with garbage.
    Set-Content -LiteralPath $pwFile -Value "REPLACE_WITH_REAL_IBKR_PASSWORD" -Encoding ASCII -NoNewline
    Write-Host "  created sentinel file (operator must overwrite with real password)"
}

# 3. Lock ACL: only trader gets read; SYSTEM gets full
& icacls.exe $pwFile /inheritance:r /grant:r ('{0}:R' -f $user) /grant:r 'SYSTEM:F' | Out-Null
Write-Host "  ACL hardened (read-only for $user, full for SYSTEM)"
Write-Host ""

Write-Host "--- Verification ---"
Write-Host "  size: $((Get-Item $pwFile).Length) bytes"
Write-Host "  ACL:"
(Get-Acl $pwFile).Access | ForEach-Object {
    Write-Host "    $($_.IdentityReference) $($_.AccessControlType) $($_.FileSystemRights)"
}
Write-Host ""
Write-Host "Operator action required to enable file-based auth fallback:"
Write-Host '  ssh forex-vps "powershell -Command \"Set-Content -LiteralPath C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibkr_pw.txt -Value REAL_PASSWORD -Encoding ASCII -NoNewline\""'
Write-Host ""
Write-Host "Then add -IbcPasswordFile to ETA-IBGateway task command (currently still uses env vars):"
Write-Host "  Update task arg to: -UseIbc -ForceRestart -ApiPort 4002 -IbcPasswordFile `"C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibkr_pw.txt`""
Write-Host ""
Write-Host "Until then: env vars (machine scope) remain the live auth source. File is a no-op."
