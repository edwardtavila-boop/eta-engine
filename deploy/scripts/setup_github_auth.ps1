# setup_github_auth.ps1
# One-shot interactive setup for VPS git auth. Run on the VPS.
#
# Two modes:
#   -Mode Token  (default) -- prompts for a Personal Access Token, pipes
#                             to "gh auth login --with-token", configures
#                             git credential helper, fetches all repos.
#   -Mode Web              -- runs "gh auth login --web", which prints a
#                             one-time device code you enter at
#                             https://github.com/login/device
#
# After either mode completes successfully, the script:
#   * runs `gh auth setup-git` so git fetch/push uses the gh-managed
#     credential helper (NOT wincredman -- this is what was failing)
#   * runs `git fetch origin` in workspace + each submodule so subsequent
#     pulls are immediate
#
# Idempotent: re-running with a valid existing login is a no-op verify pass.

[CmdletBinding()]
param(
    [ValidateSet("Token", "Web")]
    [string]$Mode = "Token",
    [string]$WorkspaceRoot = "C:\EvolutionaryTradingAlgo"
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  OK $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }

# 1. Pre-flight
Write-Step "Pre-flight"
$ghVersion = (gh --version 2>&1 | Select-Object -First 1)
if (-not $ghVersion) { throw "gh CLI not found. Install from https://cli.github.com" }
Write-Ok "gh: $ghVersion"

$reachable = (Test-NetConnection github.com -Port 443 -WarningAction SilentlyContinue).TcpTestSucceeded
if (-not $reachable) { throw "github.com:443 unreachable. Check firewall / network." }
Write-Ok "github.com:443 reachable"

# 2. Already logged in?
$status = & gh auth status 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Ok "gh is already authenticated:"
    $status | ForEach-Object { Write-Host "    $_" }
    Write-Step "Verifying credential helper + fetching"
} else {
    Write-Step "gh is not authenticated -- starting $Mode flow"

    if ($Mode -eq "Token") {
        Write-Host ""
        Write-Host "Generate a Personal Access Token at:"
        Write-Host "  https://github.com/settings/tokens/new"
        Write-Host ""
        Write-Host "Settings:"
        Write-Host "  Note:        VPS-eta-pull"
        Write-Host "  Expiration:  90 days (or your choice)"
        Write-Host "  Scopes:      [x] repo   [x] workflow"
        Write-Host ""
        $secureToken = Read-Host -Prompt "Paste the PAT (input hidden)" -AsSecureString
        $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
        try {
            $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        } finally {
            [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
        if ([string]::IsNullOrWhiteSpace($plain)) { throw "Empty PAT. Aborting." }

        $plain | & gh auth login --hostname github.com --git-protocol https --with-token
        if ($LASTEXITCODE -ne 0) { throw "gh auth login --with-token failed (exit $LASTEXITCODE). Check token scopes." }
        # Wipe the plaintext from memory ASAP.
        $plain = $null
        Write-Ok "gh authenticated via PAT"
    }
    else {
        # Web (device-code) flow.
        Write-Host ""
        Write-Host "When prompted, open the URL on any device + enter the code shown."
        Write-Host ""
        & gh auth login --hostname github.com --git-protocol https --web
        if ($LASTEXITCODE -ne 0) { throw "gh auth login --web failed (exit $LASTEXITCODE)." }
        Write-Ok "gh authenticated via device-code flow"
    }
}

# 3. Configure git to use gh as the credential helper (sidesteps wincredman).
Write-Step "Configuring git credential helper"
& gh auth setup-git
if ($LASTEXITCODE -ne 0) { throw "gh auth setup-git failed (exit $LASTEXITCODE)." }
Write-Ok "git is now wired to gh (no wincredman)"

# 4. Fetch all known repos.
Write-Step "Fetching all repos under $WorkspaceRoot"
$repos = @(
    $WorkspaceRoot,
    (Join-Path $WorkspaceRoot "eta_engine"),
    (Join-Path $WorkspaceRoot "firm"),
    (Join-Path $WorkspaceRoot "mnq_bot"),
    (Join-Path $WorkspaceRoot "mnq_backtest"),
    (Join-Path $WorkspaceRoot "firm_command_center")
)
foreach ($repo in $repos) {
    if (-not (Test-Path (Join-Path $repo ".git"))) {
        Write-Warn "skip $repo (no .git)"
        continue
    }
    Write-Host "  fetch $repo"
    & git -C $repo fetch origin --quiet 2>&1 | ForEach-Object { Write-Host "    $_" }
    if ($LASTEXITCODE -ne 0) { Write-Warn "fetch failed for $repo" }
}

# 5. Final status.
Write-Step "Final status"
& gh auth status 2>&1 | ForEach-Object { Write-Host "  $_" }
Write-Host ""
Write-Host "Next: from each repo you want to update:"
Write-Host "  git -C $WorkspaceRoot                pull origin main"
Write-Host "  git -C $WorkspaceRoot\eta_engine     pull origin main"
Write-Host "  git -C $WorkspaceRoot\firm           pull origin main"
Write-Host ""
Write-Ok "Setup complete."
