<#
.SYNOPSIS
    Install the jarvis-trading Hermes skill into the operator's local Hermes Agent.

.DESCRIPTION
    Copies the directory this script lives in into $env:USERPROFILE\.hermes\skills\jarvis-trading.
    Diagnoses if Hermes Agent isn't installed. Prompts before overwriting an existing install
    unless -Force is supplied.

.PARAMETER Force
    Overwrite an existing jarvis-trading install without prompting.

.EXAMPLE
    pwsh deploy.ps1
    pwsh deploy.ps1 -Force
#>
[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# Resolve source (the directory containing this script).
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Hermes-desktop puts skills under one of two layouts depending on the
# installer version:
#   * older / minimal:  ~/.hermes/skills/
#   * current bundled:  ~/.hermes/hermes-agent/skills/
# Probe both, prefer whichever the operator actually has.
$candidatePaths = @(
    (Join-Path $env:USERPROFILE '.hermes\hermes-agent\skills'),
    (Join-Path $env:USERPROFILE '.hermes\skills')
)
$HermesSkillsRoot = $candidatePaths | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $HermesSkillsRoot) {
    Write-Host "ERROR: Hermes skills directory not found." -ForegroundColor Red
    Write-Host "Looked at:" -ForegroundColor Red
    foreach ($p in $candidatePaths) { Write-Host "  $p" -ForegroundColor Red }
    Write-Host "Hermes not installed - run hermes-desktop first to bootstrap ~/.hermes/." -ForegroundColor Red
    Write-Host "After installing Hermes Agent, re-run this script." -ForegroundColor Red
    exit 1
}

$DestDir = Join-Path $HermesSkillsRoot 'jarvis-trading'

Write-Host "jarvis-trading deploy"
Write-Host "  source:      $ScriptDir"
Write-Host "  destination: $DestDir"
Write-Host ""

# Token warning (non-fatal).
if (-not $env:JARVIS_MCP_TOKEN) {
    Write-Host "WARNING: JARVIS_MCP_TOKEN env var is not set." -ForegroundColor Yellow
    Write-Host "Set JARVIS_MCP_TOKEN before starting Hermes Agent or JARVIS calls will fail with 401." -ForegroundColor Yellow
    Write-Host ""
}

# Prompt to overwrite.
if (Test-Path $DestDir) {
    if ($Force) {
        Write-Host "Existing install at $DestDir will be overwritten (-Force)."
    } else {
        $response = Read-Host "jarvis-trading already exists at $DestDir. Overwrite? (y/N)"
        if ($response -notmatch '^[Yy]') {
            Write-Host "Aborted. No changes made."
            exit 0
        }
    }
    Remove-Item -Recurse -Force $DestDir
}

# Copy the tree.
Copy-Item -Recurse -Force -Path $ScriptDir -Destination $DestDir

# Verify manifest landed.
$ManifestPath = Join-Path $DestDir 'manifest.yaml'
if (-not (Test-Path $ManifestPath)) {
    Write-Host "ERROR: manifest.yaml missing in destination after copy." -ForegroundColor Red
    Write-Host "Expected at: $ManifestPath" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Installed. Restart Hermes Agent to pick up the new skill." -ForegroundColor Green
exit 0
