# EVOLUTIONARY TRADING ALGO // sync_dashboard_api_live.ps1
# Safely updates only the eta_engine child checkout on the VPS, then restarts
# the canonical dashboard API task. The superproject root is inspected but not
# mutated because the live VPS root can contain runtime artifacts and local data.

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Root = "C:\EvolutionaryTradingAlgo",
    [string]$Branch = "codex/paper-live-runtime-hardening",
    [string]$TaskName = "ETA-Dashboard-API",
    [string]$ProbeUri = "http://127.0.0.1:8000/api/bot-fleet",
    [int]$ProbeDelaySeconds = 8,
    [switch]$SkipGitPull
)

$ErrorActionPreference = "Stop"

function Assert-CanonicalEtaPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
    if (
        $resolved -ne "C:\EvolutionaryTradingAlgo" -and
        -not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Refusing non-canonical ETA path: $Path"
    }
    return $resolved
}

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & git @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE in $WorkingDirectory"
        }
    }
    finally {
        Pop-Location
    }
}

$RootFull = Assert-CanonicalEtaPath -Path $Root
$EngineDir = Assert-CanonicalEtaPath -Path (Join-Path $RootFull "eta_engine")
$DashboardApi = Join-Path $EngineDir "deploy\scripts\dashboard_api.py"
$RegisterTaskScript = Join-Path $EngineDir "deploy\scripts\register_dashboard_api_task.ps1"
$VenvPython = Join-Path $EngineDir ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python.exe" }

if (-not (Test-Path -LiteralPath $EngineDir)) {
    throw "Missing eta_engine child checkout: $EngineDir"
}
if (-not (Test-Path -LiteralPath $DashboardApi)) {
    throw "Missing dashboard API module: $DashboardApi"
}

$rootDirty = $false
Push-Location -LiteralPath $RootFull
try {
    $rootPorcelain = (& git status --porcelain 2>$null)
    $rootDirty = [bool]$rootPorcelain
    if ($rootDirty) {
        Write-Warning "Root checkout has local changes; leaving superproject untouched and syncing eta_engine only."
    }
}
finally {
    Pop-Location
}

Push-Location -LiteralPath $EngineDir
try {
    $enginePorcelain = (& git status --porcelain 2>$null)
    if ($enginePorcelain) {
        throw "eta_engine checkout is dirty; refusing to pull over local work."
    }
}
finally {
    Pop-Location
}

if (-not $SkipGitPull) {
    Invoke-Git -WorkingDirectory $EngineDir -Arguments @("fetch", "origin", $Branch)
    Invoke-Git -WorkingDirectory $EngineDir -Arguments @("checkout", $Branch)
    Invoke-Git -WorkingDirectory $EngineDir -Arguments @("pull", "--ff-only", "origin", $Branch)
}

& $Python -m py_compile $DashboardApi
if ($LASTEXITCODE -ne 0) {
    throw "dashboard_api.py py_compile failed"
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    throw "Missing $TaskName. Register it with: $RegisterTaskScript -Start"
}

if ($PSCmdlet.ShouldProcess($TaskName, "Restart canonical ETA dashboard API task")) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $TaskName
}

Start-Sleep -Seconds ([Math]::Max(0, $ProbeDelaySeconds))
$probe = Invoke-RestMethod -Uri $ProbeUri -TimeoutSec 25
$botsCount = ($probe.bots | Measure-Object).Count
$exitSummaryPresent = [bool]$probe.PSObject.Properties["target_exit_summary"]
$head = ""
Push-Location -LiteralPath $EngineDir
try {
    $head = (& git rev-parse --short HEAD).Trim()
}
finally {
    Pop-Location
}

[pscustomobject]@{
    status = "ok"
    root = $RootFull
    root_dirty = $rootDirty
    engine_dir = $EngineDir
    engine_head = $head
    task = $TaskName
    probe_uri = $ProbeUri
    bots = $botsCount
    target_exit_summary = $exitSummaryPresent
    target_exit_status = $probe.target_exit_summary.status
    open_positions = $probe.target_exit_summary.open_position_count
    supervisor_exit_watch = $probe.target_exit_summary.supervisor_watch_count
    broker_router = $probe.broker_router.status
    signal_cadence = $probe.signal_cadence.status
} | ConvertTo-Json -Compress
