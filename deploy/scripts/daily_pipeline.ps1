# ETA Self-Sustainable Pipeline
# Run daily to keep bar data fresh, bots running, and system healthy

$ErrorActionPreference = "Continue"
$ROOT = "C:\EvolutionaryTradingAlgo"
$LOG = "$ROOT\var\eta_engine\logs\pipeline_$(Get-Date -Format yyyyMMdd).log"
$PY = "C:\Program Files\Python312\python.exe"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts  $msg" | Tee-Object -FilePath $LOG -Append
}

Log "=== ETA DAILY PIPELINE START ==="

# ── 1. Pull fresh crypto daily bars (yfinance) ──────────────────
Log "1. Fetching crypto daily bars..."
& $PY "$ROOT\eta_engine\scripts\fetch_crypto_daily_yahoo.py" --symbols BTC,ETH,SOL --years 1 2>&1 | ForEach-Object { Log "  $_" }

# ── 2. Sync data to ibkr mirror ─────────────────────────────────
Log "2. Syncing data to ibkr mirror..."
Copy-Item "$ROOT\data\crypto\history\*_D.csv" "$ROOT\data\crypto\ibkr\history\" -Force -ErrorAction SilentlyContinue
Copy-Item "$ROOT\eta_engine\data\crypto\history\*_D.csv" "$ROOT\data\crypto\ibkr\history\" -Force -ErrorAction SilentlyContinue
Log "  Mirror synced"

# ── 3. Check VPS services ───────────────────────────────────────
Log "3. Checking critical services..."
$services = @("FirmCommandCenter", "HermesJarvisTelegram")
foreach ($svc in $services) {
    $s = Get-Service $svc -ErrorAction SilentlyContinue
    if (-not $s) { Log "  MISSING: $svc" }
    elseif ($s.Status -ne "Running") { 
        Log "  DOWN: $svc — restarting..."
        Start-Service $svc -ErrorAction SilentlyContinue
    } else { Log "  OK: $svc" }
}

# ── 4. Check ports ──────────────────────────────────────────────
Log "4. Checking ports..."
foreach ($port in @(8000, 5000, 4002)) {
    $listening = netstat -ano 2>$null | Select-String ":$port .*LISTENING"
    if (-not $listening) { 
        Log "  DOWN: port $port"
        if ($port -eq 8000) {
            Log "  Restarting dashboard..."
            schtasks /run /tn ETA-Dashboard-Fixed 2>$null
        }
    } else { Log "  OK: port $port" }
}

# ── 5. Check critical tasks ─────────────────────────────────────
Log "5. Checking scheduled tasks..."
$tasks = @("ETA-Jarvis-Live", "ETA-Avengers-Fleet", "ETA-Dashboard-Fixed")
foreach ($tsk in $tasks) {
    $info = schtasks /query /tn $tsk /fo csv 2>$null | ConvertFrom-Csv -ErrorAction SilentlyContinue
    if (-not $info) { Log "  MISSING: $tsk" }
    elseif ($info.Status -ne "Running") {
        Log "  STOPPED: $tsk — restarting..."
        schtasks /run /tn $tsk 2>$null
    } else { Log "  OK: $tsk" }
}

# ── 6. Disk check ───────────────────────────────────────────────
Log "6. Disk space..."
$disk = Get-PSDrive C
$freeGB = [math]::Round($disk.Free / 1GB, 1)
Log "  Free: ${freeGB}GB"
if ($freeGB -lt 10) { Log "  WARNING: Low disk space!" }

# ── 7. Health snapshot ──────────────────────────────────────────
Log "7. Fleet health..."
try {
    $fleet = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/bot-fleet" -TimeoutSec 10
    $bots = $fleet.bots.Count
    $pnl = ($fleet.bots | ForEach-Object { $_.todays_pnl } | Measure-Object -Sum).Sum
    Log "  Bots: $bots, PnL: $pnl"
} catch { Log "  Dashboard unreachable" }

Log "=== PIPELINE COMPLETE ==="
