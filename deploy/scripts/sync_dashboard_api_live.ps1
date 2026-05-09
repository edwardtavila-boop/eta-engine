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
    [string]$ProxyTaskName = "ETA-Proxy-8421",
    [string]$ProxyProbeUri = "http://127.0.0.1:8421/api/bot-fleet",
    [int]$ProbeDelaySeconds = 8,
    [int]$ProbeAttempts = 4,
    [int]$ProbeTimeoutSeconds = 35,
    [int]$ProbeRetryDelaySeconds = 5,
    [switch]$SkipGitPull,
    [switch]$SkipProxyRestart
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
$rootDirtySummary = [ordered]@{
    branch = ""
    head = ""
    status_count = 0
    deleted_tracked_count = 0
    modified_tracked_count = 0
    untracked_count = 0
}
Push-Location -LiteralPath $RootFull
try {
    $rootBranch = ((& git branch --show-current 2>$null) | Select-Object -First 1)
    $rootHead = ((& git rev-parse --short HEAD 2>$null) | Select-Object -First 1)
    $rootPorcelain = @(& git status --porcelain 2>$null)
    $rootDeletedTracked = @(& git ls-files -d 2>$null)
    $rootModifiedTracked = @(& git diff --name-only --diff-filter=M 2>$null)
    $rootUntracked = @(& git ls-files --others --exclude-standard 2>$null)
    $rootDirty = $rootPorcelain.Count -gt 0
    $rootDirtySummary = [ordered]@{
        branch = "$rootBranch".Trim()
        head = "$rootHead".Trim()
        status_count = $rootPorcelain.Count
        deleted_tracked_count = $rootDeletedTracked.Count
        modified_tracked_count = $rootModifiedTracked.Count
        untracked_count = $rootUntracked.Count
    }
    if ($rootDirty) {
        Write-Warning (
            "Root checkout has local changes " +
            "(branch=$($rootDirtySummary.branch), head=$($rootDirtySummary.head), " +
            "status=$($rootDirtySummary.status_count), " +
            "deleted_tracked=$($rootDirtySummary.deleted_tracked_count), " +
            "modified_tracked=$($rootDirtySummary.modified_tracked_count), " +
            "untracked=$($rootDirtySummary.untracked_count)); " +
            "leaving superproject untouched and syncing eta_engine only."
        )
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
$probe = $null
$lastProbeError = $null
$probeAttempt = 0
$probeAttempts = [Math]::Max(1, $ProbeAttempts)
$probeTimeout = [Math]::Max(1, $ProbeTimeoutSeconds)
$probeRetryDelay = [Math]::Max(0, $ProbeRetryDelaySeconds)

for ($attempt = 1; $attempt -le $probeAttempts; $attempt++) {
    $probeAttempt = $attempt
    try {
        $probe = Invoke-RestMethod -Uri $ProbeUri -TimeoutSec $probeTimeout
        break
    }
    catch {
        $lastProbeError = $_.Exception.Message
        Write-Warning "Dashboard probe attempt $attempt of $probeAttempts failed: $lastProbeError"
        if ($attempt -lt $probeAttempts) {
            Start-Sleep -Seconds $probeRetryDelay
        }
    }
}

if (-not $probe) {
    throw "Dashboard probe failed after $probeAttempts attempt(s): $lastProbeError"
}

$proxyProbe = $null
$lastProxyProbeError = $null
$proxyProbeAttempt = 0
if (-not $SkipProxyRestart) {
    $proxyTask = Get-ScheduledTask -TaskName $ProxyTaskName -ErrorAction SilentlyContinue
    if (-not $proxyTask) {
        throw "Missing $ProxyTaskName. Register it with: deploy\scripts\register_proxy8421_bridge_task.ps1 -Start"
    }

    if ($PSCmdlet.ShouldProcess($ProxyTaskName, "Restart ETA dashboard proxy bridge task")) {
        Stop-ScheduledTask -TaskName $ProxyTaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Start-ScheduledTask -TaskName $ProxyTaskName
    }

    Start-Sleep -Seconds ([Math]::Min(3, [Math]::Max(0, $ProbeDelaySeconds)))
    for ($attempt = 1; $attempt -le $probeAttempts; $attempt++) {
        $proxyProbeAttempt = $attempt
        try {
            $proxyProbe = Invoke-RestMethod -Uri $ProxyProbeUri -TimeoutSec $probeTimeout
            break
        }
        catch {
            $lastProxyProbeError = $_.Exception.Message
            Write-Warning "Dashboard proxy probe attempt $attempt of $probeAttempts failed: $lastProxyProbeError"
            if ($attempt -lt $probeAttempts) {
                Start-Sleep -Seconds $probeRetryDelay
            }
        }
    }

    if (-not $proxyProbe) {
        throw "Dashboard proxy probe failed after $probeAttempts attempt(s): $lastProxyProbeError"
    }
}

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
    root_dirty_summary = $rootDirtySummary
    engine_dir = $EngineDir
    engine_head = $head
    task = $TaskName
    probe_uri = $ProbeUri
    probe_attempt = $probeAttempt
    probe_attempts = $probeAttempts
    probe_timeout_seconds = $probeTimeout
    proxy_task = if ($SkipProxyRestart) { $null } else { $ProxyTaskName }
    proxy_probe_uri = if ($SkipProxyRestart) { $null } else { $ProxyProbeUri }
    proxy_probe_attempt = if ($SkipProxyRestart) { $null } else { $proxyProbeAttempt }
    bots = $botsCount
    target_exit_summary = $exitSummaryPresent
    target_exit_status = $probe.target_exit_summary.status
    open_positions = $probe.target_exit_summary.open_position_count
    supervisor_open_positions = $probe.target_exit_summary.supervisor_local_position_count
    broker_open_positions = $probe.target_exit_summary.broker_open_position_count
    broker_position_scope = $probe.target_exit_summary.broker_position_scope
    missing_brackets = $probe.target_exit_summary.missing_bracket_count
    supervisor_exit_watch = $probe.target_exit_summary.supervisor_watch_count
    broker_router = $probe.broker_router.status
    signal_cadence = $probe.signal_cadence.status
} | ConvertTo-Json -Compress
