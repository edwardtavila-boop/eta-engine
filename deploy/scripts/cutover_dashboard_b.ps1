# deploy/scripts/cutover_dashboard_b.ps1
# ============================================================
# Stage 2 cutover: make dashboard_api.py the live operator surface.
#
# Run on the VPS from the eta_engine repo root:
#   powershell -ExecutionPolicy Bypass -File deploy\scripts\cutover_dashboard_b.ps1
# ============================================================
$ErrorActionPreference = "Stop"
$EtaEngineDir = $PSScriptRoot | Split-Path -Parent | Split-Path -Parent
$Python = Join-Path $EtaEngineDir ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

function Write-Log  { param($m) Write-Host "[cutover] $m" -ForegroundColor Cyan }
function Write-OK   { param($m) Write-Host "[ OK  ] $m" -ForegroundColor Green }
function Write-Fail { param($m) Write-Host "[FAIL ] $m" -ForegroundColor Red }
function Die        { param($m) Write-Fail $m; exit 1 }

# ------------------------------------------------------------------
# 1. Git pull
# ------------------------------------------------------------------
Write-Log "Step 1: git pull..."
$pull = & git -C $EtaEngineDir pull --ff-only 2>&1 | Out-String
Write-Log $pull.Trim()
$sha = & git -C $EtaEngineDir rev-parse --short HEAD
Write-OK "HEAD is now $sha"

# ------------------------------------------------------------------
# 2. Kill whatever is on port 8420
# ------------------------------------------------------------------
Write-Log "Step 2: clearing port 8420..."
$conns = Get-NetTCPConnection -LocalPort 8420 -State Listen -ErrorAction SilentlyContinue
if ($conns) {
    foreach ($c in $conns) {
        $pid_ = $c.OwningProcess
        Write-Log "  killing PID $pid_ on port 8420"
        Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
    $still = Get-NetTCPConnection -LocalPort 8420 -State Listen -ErrorAction SilentlyContinue
    if ($still) { Die "port 8420 still occupied after kill" }
    Write-OK "port 8420 cleared"
} else {
    Write-Log "  port 8420 was already free"
}

# ------------------------------------------------------------------
# 3. Re-register Eta-Dashboard scheduled task
# ------------------------------------------------------------------
Write-Log "Step 3: registering Eta-Dashboard task..."
$taskName = "Eta-Dashboard"
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "-m uvicorn eta_engine.deploy.scripts.dashboard_api:app --host 127.0.0.1 --port 8420" `
    -WorkingDirectory $EtaEngineDir

$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -User $env:USERNAME `
    -RunLevel Limited | Out-Null
Write-OK "task $taskName registered"

# ------------------------------------------------------------------
# 4. Start the task immediately
# ------------------------------------------------------------------
Write-Log "Step 4: starting $taskName..."
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 4
Write-OK "task started"

# ------------------------------------------------------------------
# 5. Health-check loop (30 s)
# ------------------------------------------------------------------
Write-Log "Step 5: health check..."
$up = $false
for ($i = 0; $i -lt 15; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8420/health" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $up = $true; break }
    } catch {}
    Start-Sleep -Seconds 2
}
if (-not $up) { Die "/health never returned 200 after 30 s — check logs" }
Write-OK "/health OK"

# ------------------------------------------------------------------
# 6. Bot-fleet check
# ------------------------------------------------------------------
Write-Log "Step 6: bot-fleet check..."
try {
    $r2 = Invoke-WebRequest -Uri "http://127.0.0.1:8420/api/bot-fleet" -UseBasicParsing -TimeoutSec 5
    $j  = $r2.Content | ConvertFrom-Json
    $n  = $j.confirmed_bots
    $names = ($j.bots | Select-Object -ExpandProperty name) -join ", "
    Write-OK "confirmed_bots=$n  bots=[$names]"
} catch {
    Write-Fail "/api/bot-fleet error: $_"
    Write-Log  "Dashboard is live but supervisor data may be missing — check heartbeat path."
}

# ------------------------------------------------------------------
# 7. Write cutover receipt
# ------------------------------------------------------------------
Write-Log "Step 7: writing receipt..."
$stateDir = Join-Path $EtaEngineDir "state\ops"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$receipt = @{
    event       = "cutover_dashboard_b"
    ts          = (Get-Date).ToString("o")
    git_sha     = $sha
    port        = 8420
    task        = $taskName
    confirmed_bots = if ($n) { $n } else { "unknown" }
    status      = "success"
} | ConvertTo-Json
Set-Content -Path (Join-Path $stateDir "cutover_dashboard_b.json") -Value $receipt -Encoding UTF8
Write-OK "receipt written to state/ops/cutover_dashboard_b.json"

Write-Host ""
Write-Host "======================================================" -ForegroundColor Green
Write-Host "  Cutover complete.  app.evolutionarytradingalgo.com  " -ForegroundColor Green
Write-Host "  is now served by dashboard_api.py on port 8420.     " -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Green
