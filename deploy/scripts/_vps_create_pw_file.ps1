$ErrorActionPreference = "Stop"
$pwFile = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibkr_pw.txt"
$user = "trader"

Write-Host "--- Creating fallback IBC password file on VPS ---"
Write-Host "  path : $pwFile"
Write-Host "  user : $user"
Write-Host ""

$parent = Split-Path -Parent $pwFile
if (-not (Test-Path $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Write-Host "  created parent dir: $parent"
}

if (Test-Path $pwFile) {
    Write-Host "  pw file already exists - leaving content alone"
} else {
    Set-Content -LiteralPath $pwFile -Value "REPLACE_WITH_REAL_IBKR_PASSWORD" -Encoding ASCII -NoNewline
    Write-Host "  created sentinel file (operator must overwrite with real password)"
}

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
Write-Host "Operator action to enable file-based auth fallback:"
Write-Host "  1. Write real password to file (in-place edit on VPS)"
Write-Host "  2. Add -IbcPasswordFile to ETA-IBGateway task command"
Write-Host ""
Write-Host "Until then env vars (machine scope) remain the live auth source. File is a no-op."
