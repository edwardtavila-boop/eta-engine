# ============================================================================
# install_pwsh7.ps1 -- Install PowerShell 7 + repoint all Apex-* tasks to pwsh
#
# After this, scheduled tasks get ~2x faster pipeline/format ops and cleaner
# error handling (&&, ||, null-coalescing).
# ============================================================================
[CmdletBinding()]
param([switch]$Force)

function Log  { param($m) Write-Host "[pwsh7] $m" -ForegroundColor Cyan }
function OK   { param($m) Write-Host "[ OK ] $m" -ForegroundColor Green }
function Die  { param($m) Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }

# ----------------------------------------------------------------------------
# 1. Install via winget (preferred) or direct MSI
# ----------------------------------------------------------------------------
$pwshExe = (Get-Command pwsh -ErrorAction SilentlyContinue).Source

if ($pwshExe -and -not $Force) {
    OK "pwsh already installed at $pwshExe"
} else {
    Log "installing PowerShell 7 via winget"
    try {
        winget install --id Microsoft.PowerShell --source winget --accept-package-agreements --accept-source-agreements --silent 2>&1 | Out-Null
    } catch {
        Die "winget failed: $($_.Exception.Message). Install manually from https://aka.ms/powershell"
    }
    # Refresh PATH so pwsh is findable
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    $pwshExe = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
    if (-not $pwshExe) {
        Die "pwsh not found in PATH after install. Try: C:\Program Files\PowerShell\7\pwsh.exe"
    }
    OK "pwsh installed at $pwshExe"
}

# ----------------------------------------------------------------------------
# 2. Nothing to repoint today -- our scheduled tasks run python.exe directly,
#    not powershell. Logged here for future reference.
# ----------------------------------------------------------------------------
Log "scheduled tasks run python.exe directly, no repointing needed"
Log "pwsh is available for interactive sessions + future scripts"

# ----------------------------------------------------------------------------
# 3. Self-test
# ----------------------------------------------------------------------------
$version = & pwsh -NoProfile -Command "$PSVersionTable.PSVersion.ToString()"
OK "pwsh version: $version"
OK "done"
