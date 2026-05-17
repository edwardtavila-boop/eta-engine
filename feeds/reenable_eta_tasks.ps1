<#
.SYNOPSIS
  Compatibility wrapper for ``eta_engine\scripts\reenable_eta_tasks.ps1``.

.DESCRIPTION
  Keeps legacy feed entrypoints working while the canonical implementation
  lives under ``eta_engine\scripts``. This avoids silent drift between two
  copies of the same scheduled-task repair flow.
#>

param(
    [switch]$DryRun,
    [switch]$DeleteStale
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$canonicalScript = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "..\scripts\reenable_eta_tasks.ps1")
)

if (-not (Test-Path -LiteralPath $canonicalScript)) {
    throw "Canonical re-enable task script not found: $canonicalScript"
}

$forwardArgs = @()
if ($DryRun) {
    $forwardArgs += "-DryRun"
}
if ($DeleteStale) {
    $forwardArgs += "-DeleteStale"
}

& $canonicalScript @forwardArgs
exit $LASTEXITCODE
