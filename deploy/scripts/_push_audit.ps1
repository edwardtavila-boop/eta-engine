$ErrorActionPreference = "Stop"

$root = "C:\EvolutionaryTradingAlgo"
$audit = Join-Path $root "eta_engine\deploy\scripts\ceiling_audit.py"

if (-not (Test-Path -LiteralPath $audit)) {
    throw "ETA ceiling audit script not found at $audit"
}

& python $audit
exit $LASTEXITCODE
