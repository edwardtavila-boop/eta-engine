@echo off
setlocal

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "ETA_STATE_DIR=%ETA_ROOT%\var\eta_engine\state"
set "ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine"
set "ETA_DASHBOARD_HOST=127.0.0.1"
set "ETA_DASHBOARD_PORT=8000"
set "PYTHONPATH=%ETA_ROOT%;%ETA_ENGINE%;%ETA_ENGINE%\src"
set "PYTHONUNBUFFERED=1"

set "PYTHON_EXE=%ETA_ENGINE%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python.exe"

if not exist "%ETA_STATE_DIR%" mkdir "%ETA_STATE_DIR%"
if not exist "%ETA_LOG_DIR%" mkdir "%ETA_LOG_DIR%"
cd /d "%ETA_ROOT%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$port=8000; Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { try { Stop-Process -Id $_ -Force -ErrorAction Stop } catch {} }" ^
    1>> "%ETA_LOG_DIR%\dashboard_api.stdout.log" ^
    2>> "%ETA_LOG_DIR%\dashboard_api.stderr.log"

"%PYTHON_EXE%" -m uvicorn eta_engine.deploy.scripts.dashboard_api:app ^
    --host "%ETA_DASHBOARD_HOST%" ^
    --port "%ETA_DASHBOARD_PORT%" ^
    1>> "%ETA_LOG_DIR%\dashboard_api.stdout.log" ^
    2>> "%ETA_LOG_DIR%\dashboard_api.stderr.log"

exit /b %ERRORLEVEL%
