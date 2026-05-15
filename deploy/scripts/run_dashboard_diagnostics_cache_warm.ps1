# EVOLUTIONARY TRADING ALGO // run_dashboard_diagnostics_cache_warm.ps1
# Keeps the local dashboard diagnostics cache warm for faster public first paint.

[CmdletBinding()]
param(
    [string]$Url = "http://127.0.0.1:8421/api/dashboard/diagnostics?refresh=1",
    [int]$Iterations = 3,
    [int]$SleepSeconds = 20
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$WorkspaceRoot = "C:\EvolutionaryTradingAlgo"
$LogDir = Join-Path $WorkspaceRoot "logs\eta_engine"
$LogPath = Join-Path $LogDir "dashboard_diagnostics_cache_warm.task.log"

function Assert-CanonicalEtaPath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing non-canonical ETA path: $Path"
    }
}

Assert-CanonicalEtaPath -Path $LogDir
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if ($Iterations -lt 1) {
    $Iterations = 1
}
if ($SleepSeconds -lt 5) {
    $SleepSeconds = 5
}

for ($i = 1; $i -le $Iterations; $i++) {
    $started = Get-Date
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 25
        $sw.Stop()
        $cacheStatus = "unknown"
        try {
            $payload = $response.Content | ConvertFrom-Json
            if ($payload.diagnostics_cache -and $payload.diagnostics_cache.status) {
                $cacheStatus = [string]$payload.diagnostics_cache.status
            }
        } catch {
            $cacheStatus = "json_unreadable"
        }
        Add-Content -Path $LogPath -Encoding UTF8 -Value (
            "{0} dashboard_diagnostics_cache_warm iteration={1}/{2} status={3} ms={4} bytes={5} cache={6}" -f
            $started.ToString("o"), $i, $Iterations, [int]$response.StatusCode, $sw.ElapsedMilliseconds, $response.Content.Length, $cacheStatus
        )
    } catch {
        $sw.Stop()
        Add-Content -Path $LogPath -Encoding UTF8 -Value (
            "{0} dashboard_diagnostics_cache_warm iteration={1}/{2} error_ms={3} error={4}" -f
            $started.ToString("o"), $i, $Iterations, $sw.ElapsedMilliseconds, $_.Exception.Message
        )
    }

    if ($i -lt $Iterations) {
        Start-Sleep -Seconds $SleepSeconds
    }
}

# This is a read-only warmer. The log is the signal; do not mark Task Scheduler
# unhealthy for a transient cache miss while the dashboard API restarts.
exit 0
