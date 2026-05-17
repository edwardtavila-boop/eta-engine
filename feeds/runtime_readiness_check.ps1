<#
.SYNOPSIS
  Compatibility wrapper for ``eta_engine\scripts\runtime_readiness_check.ps1``.

.DESCRIPTION
  Keeps legacy feed entrypoints working while the canonical implementation
  lives under ``eta_engine\scripts``. This avoids silent drift between two
  copies of the same runtime-readiness audit.
#>

param(
    [switch]$Json,
    [switch]$Fast
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$canonicalScript = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "..\scripts\runtime_readiness_check.ps1")
)

if (-not (Test-Path -LiteralPath $canonicalScript)) {
    throw "Canonical runtime readiness script not found: $canonicalScript"
}

$forwardArgs = @()
if ($Json) {
    $forwardArgs += "-Json"
}
if ($Fast) {
    $forwardArgs += "-Fast"
}

& $canonicalScript @forwardArgs
exit $LASTEXITCODE
