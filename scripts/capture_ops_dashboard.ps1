param(
    [string]$Url = "https://ops.evolutionarytradingalgo.com/",
    [string]$OutFile = "C:\EvolutionaryTradingAlgo\tmp\ops_dashboard_capture.png",
    [string]$Selector = "",
    [int]$WaitMs = 0
)

$nodeCandidates = @(
    $env:ETA_NODE_BIN,
    (Get-Command node -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe")
) | Where-Object { $_ -and (Test-Path $_) }

if (-not $nodeCandidates) {
    throw "No Node.js runtime found. Set ETA_NODE_BIN or install Node.js."
}

$node = $nodeCandidates[0]
$scriptPath = "C:\EvolutionaryTradingAlgo\eta_engine\scripts\playwright_capture.js"

& $node $scriptPath $Url $OutFile $Selector $WaitMs
