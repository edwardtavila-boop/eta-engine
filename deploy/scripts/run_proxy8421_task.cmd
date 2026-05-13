@echo off
setlocal

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine"
set "ETA_PROXY_HOST=127.0.0.1"
set "ETA_PROXY_PORT=8421"
set "ETA_PROXY_TARGET=http://127.0.0.1:8000"
set "PYTHONUNBUFFERED=1"

set "PYTHON_EXE=%ETA_ENGINE%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python.exe"

if not exist "%ETA_LOG_DIR%" mkdir "%ETA_LOG_DIR%"
cd /d "%ETA_ENGINE%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$port=8421; Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { try { Stop-Process -Id $_ -Force -ErrorAction Stop } catch {} }" ^
    1>> "%ETA_LOG_DIR%\proxy_8421.stdout.log" ^
    2>> "%ETA_LOG_DIR%\proxy_8421.stderr.log"

"%PYTHON_EXE%" deploy\scripts\reverse_proxy_bridge.py ^
    --listen-host "%ETA_PROXY_HOST%" ^
    --listen-port "%ETA_PROXY_PORT%" ^
    --target "%ETA_PROXY_TARGET%" ^
    --timeout 60 ^
    1>> "%ETA_LOG_DIR%\proxy_8421.stdout.log" ^
    2>> "%ETA_LOG_DIR%\proxy_8421.stderr.log"

exit /b %ERRORLEVEL%
