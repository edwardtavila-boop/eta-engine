<#
.SYNOPSIS
  ETA Full Systems Diagnostics — run on the VPS for a complete health check.
  One-shot readout covering all processes, tasks, services, ports, IBKR,
  DeepSeek, Cloudflare, logs, and engine health.
#>

$ErrorActionPreference = "SilentlyContinue"
$PASS = 0; $FAIL = 0; $WARN = 0

function Say($label, $ok) {
    if ($ok -eq $true)  { Write-Host "  [PASS]" -ForegroundColor Green  -NoNewline; $global:PASS++ }
    elseif ($ok -eq $null -or $ok -eq -1) { Write-Host "  [WARN]" -ForegroundColor Yellow -NoNewline; $global:WARN++ }
    else                { Write-Host "  [FAIL]" -ForegroundColor Red    -NoNewline; $global:FAIL++ }
    Write-Host " $label" -ForegroundColor White
}

# ── 1. PROCESSES ──────────────────────────────────────────────
Write-Host "`n=== 1. RUNNING PROCESSES ===" -ForegroundColor Cyan
$py = (Get-Process python* -ErrorAction SilentlyContinue).Count
Say "Python processes running ($py found)" ($py -gt 2)

$java = (Get-Process java* -ErrorAction SilentlyContinue).Count
Say "Java (IBKR Gateway) running ($java found)" ($java -gt 0)

$cf = (Get-Process cloudflared* -ErrorAction SilentlyContinue).Count
Say "Cloudflared running ($cf found)" ($cf -gt 0)

$caddy = (Get-Process caddy* -ErrorAction SilentlyContinue).Count
Say "Caddy edge proxy running ($caddy found)" ($caddy -gt 0)


# ── 2. SCHEDULED TASKS ────────────────────────────────────────
Write-Host "`n=== 2. SCHEDULED TASKS ===" -ForegroundColor Cyan
$tasks = @(
    "ETA-Dashboard","ETA-Jarvis-Live","ETA-Avengers-Fleet",
    "ETA-Executor-DashboardAssemble","ETA-Executor-LogCompact","ETA-Executor-PromptWarmup",
    "ETA-Executor-AuditSummarize","ETA-Executor-LogRotate","ETA-Executor-DiskCleanup",
    "ETA-Executor-PrometheusExport",
    "ETA-Steward-ShadowTick","ETA-Steward-DriftSummary","ETA-Steward-KaizenRetro",
    "ETA-Steward-DistillTrain","ETA-Steward-MetaUpgrade","ETA-Steward-HealthWatchdog",
    "ETA-Steward-SelfTest","ETA-Steward-Backup",
    "ETA-Reasoner-TwinVerdict","ETA-Reasoner-StrategyMine","ETA-Reasoner-CausalReview",
    "ETA-Reasoner-DoctrineReview",
    "ETA-HealthCheck","ETA-Quantum-Daily-Rebalance",
    "ETA-DeepSeek-MachineGate","ETA-DeepSeek-CodexLane","ETA-DeepSeek-Combined",
    "ETA-Hermes-Jarvis-Flush","ApexIbkrGatewayWatchdog",
    "ETA-BTC-Fleet","ETA-MNQ-Supervisor",
    "ETA-Cloudflare-Tunnel","ETA-Cloudflare-Quick-Tunnel","ETA-Dashboard-Live"
)

$running = 0; $stopped = 0; $missing = 0
foreach ($t in $tasks) {
    $info = schtasks /query /tn $t /fo csv 2>$null | ConvertFrom-Csv -ErrorAction SilentlyContinue
    if (-not $info) { $missing++ }
    elseif ($info.Status -eq "Ready") { $running++; $stopped++ }
    elseif ($info.Status -eq "Running") { $running++ }
    else { $stopped++ }
}
Say "Scheduled tasks: $running running, $stopped ready/stopped, $missing missing" ($missing -eq 0)

# Show missing task names
if ($missing -gt 0) {
    foreach ($t in $tasks) {
        $info = schtasks /query /tn $t /fo csv 2>$null | ConvertFrom-Csv -ErrorAction SilentlyContinue
        if (-not $info) { Write-Host "         MISSING: $t" -ForegroundColor Red }
    }
}


# ── 3. WINDOWS SERVICES ───────────────────────────────────────
Write-Host "`n=== 3. WINDOWS SERVICES ===" -ForegroundColor Cyan
$services = @("FirmCore","FirmWatchdog","FirmCommandCenter","FirmCommandCenterTunnel",
              "FirmCommandCenterEdge","HermesJarvisTelegram")
foreach ($svc in $services) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if (-not $s) { Say "Service $svc" $false }
    else { Say "Service $svc ($($s.Status))" ($s.Status -eq "Running") }
}


# ── 4. PORTS ──────────────────────────────────────────────────
Write-Host "`n=== 4. PORT LISTENING ===" -ForegroundColor Cyan
$ports = @{
    4002 = "IBKR TWS API"
    8000 = "Dashboard API"
    8421 = "Dashboard proxy"
    8422 = "Force Multiplier status"
}
foreach ($p in $ports.Keys) {
    $listening = netstat -ano 2>$null | Select-String "LISTENING" | Select-String ":$p "
    Say "Port $p ($($ports[$p]))" ($listening -ne $null)
}


# ── 5. IBKR GATEWAY API ───────────────────────────────────────
Write-Host "`n=== 5. IBKR GATEWAY CONNECTIVITY ===" -ForegroundColor Cyan
$ibkrTws = netstat -ano 2>$null | Select-String "LISTENING" | Select-String ":4002 "
Say "IBKR TWS API listening on 127.0.0.1:4002" ($ibkrTws -ne $null)


# ── 6. DEEPSEEK API ───────────────────────────────────────────
Write-Host "`n=== 6. DEEPSEEK API KEY ===" -ForegroundColor Cyan
$envFile = Join-Path $env:USERPROFILE "eta_engine\.env"
if (Test-Path $envFile) {
    $key = Select-String -Path $envFile -Pattern "DEEPSEEK_API_KEY=(\S+)" | ForEach-Object { $_.Matches.Groups[1].Value }
    if ($key) { Say "DeepSeek API key present in .env" $true }
    else { Say "DeepSeek API key missing in .env" $false }
} else {
    $envFileAlt = "C:\EvolutionaryTradingAlgo\eta_engine\.env"
    if (Test-Path $envFileAlt) {
        $key = Select-String -Path $envFileAlt -Pattern "DEEPSEEK_API_KEY=(\S+)" | ForEach-Object { $_.Matches.Groups[1].Value }
        if ($key) { Say "DeepSeek API key present in .env" $true }
        else { Say "DeepSeek API key missing" $false }
    } else { Say ".env file not found" $false }
}


# ── 7. DASHBOARD API ─────────────────────────────────────────
Write-Host "`n=== 7. DASHBOARD / API HEALTH ===" -ForegroundColor Cyan
try {
    $api = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/bot-fleet" -TimeoutSec 10 -ErrorAction Stop
    if ($api.StatusCode -eq 200) {
        try {
            $data = $api.Content | ConvertFrom-Json
            $botCount = ($data | Get-Member -MemberType NoteProperty).Count
            Say "Bot fleet API: OK ($botCount bots returned)" $true

            $active = 0; $errors = 0
            foreach ($prop in $data.PSObject.Properties) {
                $status = $prop.Value.status
                if ($status -eq "active" -or $status -eq "paper_sim") { $active++ }
                if ($status -eq "error") { $errors++ }
            }
            Write-Host "         Active/sim: $active  |  Error: $errors" -ForegroundColor Gray
            Say "Bots not in error state" ($errors -eq 0)
        } catch {
            Say "Bot fleet returned 200 but JSON parse failed" $false
        }
    }
} catch {
    Say "Dashboard API unreachable (port 8000)" $false
}


# ── 8. LOGS ───────────────────────────────────────────────────
Write-Host "`n=== 8. ENGINE LOGS (recent entries) ===" -ForegroundColor Cyan
$logDirs = @(
    "C:\EvolutionaryTradingAlgo\eta_engine\var\logs",
    "C:\EvolutionaryTradingAlgo\firm_command_center\var\logs"
)
foreach ($dir in $logDirs) {
    if (-not (Test-Path $dir)) { continue }
    $logs = Get-ChildItem $dir -Filter "*.log" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 3
    foreach ($log in $logs) {
        $age = [int]((Get-Date) - $log.LastWriteTime).TotalMinutes
        $fresh = $age -lt 60
        $mark = if ($fresh) { "[PASS]" } else { "[WARN]" }
        $color = if ($fresh) { "Green" } else { "Yellow" }
        Write-Host "  $mark" -ForegroundColor $color -NoNewline
        Write-Host " $($log.Name) (modified ${age}m ago)" -ForegroundColor White
        # Show last 2 non-empty lines
        $lines = Get-Content $log.FullName -Tail 5 -ErrorAction SilentlyContinue |
                 Where-Object { $_.Trim() -ne "" } | Select-Object -Last 2
        foreach ($l in $lines) { Write-Host "           $l" -ForegroundColor DarkGray }
    }
}


# ── 9. PYTHON IMPORT SMOKE ────────────────────────────────────
Write-Host "`n=== 9. PYTHON MODULE SMOKE ===" -ForegroundColor Cyan
$venvPython = "C:\EvolutionaryTradingAlgo\eta_engine\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    $venvPython = "python.exe"
}
if (Test-Path $venvPython) {
    $test = & $venvPython -c "from eta_engine.strategies import per_bot_registry; from eta_engine.scripts import workspace_roots; print('import OK')" 2>&1
    if ($test -eq "import OK") { Say "Core Python imports (per_bot_registry, workspace_roots)" $true }
    else { Say "Core Python imports failed: $test" $false }
} else {
    Say "Python venv not found" $false
}


# ── 10. DISK ──────────────────────────────────────────────────
Write-Host "`n=== 10. DISK SPACE ===" -ForegroundColor Cyan
$disk = Get-PSDrive C
$freeGB = [math]::Round($disk.Free / 1GB, 1)
Say "Free disk space: ${freeGB}GB" ($freeGB -gt 5)


# ── 11. HEARTBEAT FRESHNESS ───────────────────────────────────
Write-Host "`n=== 11. ENGINE HEARTBEAT ===" -ForegroundColor Cyan
$healthPath = "C:\EvolutionaryTradingAlgo\eta_engine\data\runtime_supervisor_health.json"
if (Test-Path $healthPath) {
    try {
        $health = Get-Content $healthPath -Raw | ConvertFrom-Json
        $lastBeat = $health.last_heartbeat
        if ($lastBeat) {
            $ageMin = [math]::Round(((Get-Date) - [datetime]$lastBeat).TotalMinutes, 1)
            $fresh = $ageMin -lt 15
            $mark = if ($fresh) { "PASS" } else { "WARN" }
            Say "Last heartbeat: $lastBeat (${ageMin}m ago) [$mark]" $fresh
        } else { Say "Heartbeat field missing" $false }
    } catch { Say "Health JSON parse failed" $false }
} else {
    Say "Health file not found: $healthPath" $false
}


# ── 12. JARVIS STATE ─────────────────────────────────────────
Write-Host "`n=== 12. JARVIS STATE ===" -ForegroundColor Cyan
$jarvisPath = "C:\EvolutionaryTradingAlgo\eta_engine\data\jarvis_memory.json"
if (Test-Path $jarvisPath) {
    $ageMin = [math]::Round(((Get-Date) - (Get-Item $jarvisPath).LastWriteTime).TotalMinutes, 1)
    $fresh = $ageMin -lt 30
    $mark = if ($fresh) { "PASS" } else { "WARN" }
    Say "Jarvis memory updated ${ageMin}m ago [$mark]" $fresh
    try {
        $j = Get-Content $jarvisPath -Raw | ConvertFrom-Json
        $mode = $j.eta_mode
        $stress = $j.system_stress
        if ($mode) { Write-Host "         Mode: $mode" -ForegroundColor Gray }
        if ($null -ne $stress) { Write-Host "         Stress: $stress" -ForegroundColor Gray }
    } catch {}
} else {
    Say "Jarvis memory file not found" $false
}


# ── SUMMARY ──────────────────────────────────────────────────
Write-Host "`n=== DIAGNOSTICS SUMMARY ===" -ForegroundColor Cyan
Write-Host "  PASS: $PASS  |  WARN: $WARN  |  FAIL: $FAIL" -ForegroundColor White
if ($FAIL -eq 0 -and $WARN -le 2) {
    Write-Host "  VERDICT: HEALTHY" -ForegroundColor Green
} elseif ($FAIL -eq 0) {
    Write-Host "  VERDICT: DEGRADED (warnings present)" -ForegroundColor Yellow
} else {
    Write-Host "  VERDICT: UNHEALTHY (failures above)" -ForegroundColor Red
}
Write-Host ""
