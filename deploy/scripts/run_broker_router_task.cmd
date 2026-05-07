@echo off
setlocal

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine"
set "ETA_BROKER_ROUTER_INTERVAL_S=5"
set "ETA_BROKER_ROUTER_PENDING_DIR=%ETA_ROOT%\var\eta_engine\state\router\pending"
set "ETA_BROKER_ROUTER_STATE_ROOT=%ETA_ROOT%\var\eta_engine\state\router"
set "ETA_BROKER_ROUTER_ENFORCE_READINESS=1"
rem Keep router IBKR sessions isolated from the supervisor and machine env.
set "ETA_IBKR_CLIENT_ID=188"
set "ETA_IBKR_SUBMIT_CONFIRM_SECONDS=10"

set "PYTHON_EXE=%ETA_ENGINE%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python.exe"

if not exist "%ETA_LOG_DIR%" mkdir "%ETA_LOG_DIR%"
cd /d "%ETA_ENGINE%"

"%PYTHON_EXE%" scripts\broker_router.py ^
    1>> "%ETA_LOG_DIR%\broker_router.stdout.log" ^
    2>> "%ETA_LOG_DIR%\broker_router.stderr.log"

exit /b %ERRORLEVEL%
