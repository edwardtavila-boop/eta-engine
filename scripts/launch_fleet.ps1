# ETA Fleet Launcher — single command to start everything
$ErrorActionPreference = "Continue"
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " ETA FLEET LAUNCHER" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# 1. Start IBKR Gateway
Write-Host "`n[1/5] Starting IBKR Gateway..." -ForegroundColor Yellow
$gwDir = "C:\EvolutionaryTradingAlgo\firm_command_center\services\clientportal.gw\bin"
if (Test-Path "$gwDir\run.bat") {
    Start-Process cmd -ArgumentList "/c `"$gwDir\run.bat`"" -WindowStyle Minimized -WorkingDirectory $gwDir
    Write-Host "  Gateway starting (waiting 45s for auth)..." -ForegroundColor Gray
    Start-Sleep 45
} else {
    Write-Host "  GATEWAY NOT FOUND at $gwDir" -ForegroundColor Red
}

# 2. Verify gateway
Write-Host "`n[2/5] Checking IBKR Gateway..." -ForegroundColor Yellow
$gwCheck = Invoke-RestMethod "https://127.0.0.1:5000/v1/api/portfolio/accounts" -SkipCertificateCheck -TimeoutSec 10 -ErrorAction SilentlyContinue
if ($gwCheck) { Write-Host "  Gateway ONLINE — accounts found" -ForegroundColor Green }
else { Write-Host "  Gateway not ready — continuing..." -ForegroundColor Yellow }
Write-Host "`n[3/5] Starting trading tasks..." -ForegroundColor Yellow
$tasks = schtasks /query /fo CSV /v 2>$null | ConvertFrom-Csv | Where-Object { $_.TaskName -like "ETA-*Directional*" -or $_.TaskName -like "ETA-*Grid*" -or $_.TaskName -like "ETA-Eco-*" -or $_.TaskName -like "ETA-Avengers*" -or $_.TaskName -like "ETA-Jarvis*" -or $_.TaskName -like "ETA-Hermes*" -or $_.TaskName -like "ETA-Health*" -or $_.TaskName -like "ETA-Fleet*" -or $_.TaskName -like "ETA-Log*" -or $_.TaskName -like "ETA-Prometheus*" -or $_.TaskName -like "ETA-Quantum*" }
foreach ($t in $tasks) {
    try {
        Start-ScheduledTask -TaskName $t.TaskName -ErrorAction Stop
        Write-Host "  Started: $($t.TaskName)" -ForegroundColor Green
    } catch {
        Write-Host "  Skipped: $($t.TaskName) ($_)" -ForegroundColor Gray
    }
}

# 4. Start dashboard if not running
Write-Host "`n[4/5] Ensuring dashboard is running..." -ForegroundColor Yellow
$dashRunning = netstat -ano | findstr ":8000" | findstr "LISTENING"
if (-not $dashRunning) {
    cd C:\EvolutionaryTradingAlgo\eta_engine
    $env:PYTHONPATH = "C:\EvolutionaryTradingAlgo"
    Start-Process -NoNewWindow .venv\Scripts\python.exe -ArgumentList "-m","uvicorn","deploy.scripts.dashboard_api:app","--host","127.0.0.1","--port","8000"
    Write-Host "  Dashboard started" -ForegroundColor Green
} else {
    Write-Host "  Dashboard already running" -ForegroundColor Green
}

# 5. Wait and verify
Write-Host "`n[5/5] Waiting 30s for bots to connect..." -ForegroundColor Yellow
Start-Sleep 30

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host " FLEET STATUS" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

try {
    $r = Invoke-RestMethod "http://127.0.0.1:8000/api/bot-fleet" -TimeoutSec 10
    $active = ($r.bots | Where-Object { $_.status -ne "idle" }).Count
    $modes = ($r.bots | ForEach-Object { $_.mode } | Group-Object).Name -join ", "
    $verdicts = ($r.bots | Where-Object { $_.last_jarvis_verdict -notin @("","DENIED") }).Count
    Write-Host "  Total bots:     $($r.bots.Count)" -ForegroundColor Cyan
    Write-Host "  Active:         $active" -ForegroundColor Cyan
    Write-Host "  Modes:          $modes" -ForegroundColor Cyan
    Write-Host "  Non-DENIED:     $verdicts" -ForegroundColor Cyan
    Write-Host "`n  Dashboard: https://jarvis.evolutionarytradingalgo.com" -ForegroundColor Green
} catch {
    Write-Host "  Dashboard not responding" -ForegroundColor Red
}
