@echo off
setlocal

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine"
set "ETA_SUPERVISOR_MODE=paper_live"
set "ETA_SUPERVISOR_FEED=composite"
rem broker_router: writes pending_order JSONs to ETA_BROKER_ROUTER_PENDING_DIR;
rem the broker_router service consumes them and routes per bot_broker_routing.yaml
rem (crypto bots -> alpaca, futures -> ibkr). Was direct_ibkr; switched 2026-05-05
rem so crypto bots actually flow through Alpaca paper instead of the
rem direct_ibkr crypto-paper short-circuit (line ~884 of supervisor).
set "ETA_PAPER_LIVE_ORDER_ROUTE=broker_router"
rem ETA_PAPER_LIVE_ALLOWED_SYMBOLS applies before both direct_ibkr and
rem broker_router submission. Keep crypto paused here until Alpaca paper keys
rem are seeded; route only US futures through the live IBKR Gateway.
set "ETA_PAPER_LIVE_ALLOWED_SYMBOLS=MNQ,MNQ1,NQ,NQ1,ES,ES1,MES,MES1,RTY,RTY1,M2K,M2K1,YM,YM1,GC,GC1,MGC,MGC1,CL,CL1,MCL,MCL1,NG,NG1,ZN,ZN1,6E,6E1,M6E,M6E1"
set "ETA_SUPERVISOR_STARTING_CASH=50000"
set "ETA_BROKER_ROUTER_PENDING_DIR=%ETA_ROOT%\var\eta_engine\state\router\pending"
rem IBKR Gateway can take several seconds to promote bracket legs from PendingSubmit.
set "ETA_IBKR_SUBMIT_CONFIRM_SECONDS=10"
rem Dedicated positive order-entry client id. Do not inherit machine-level 0.
set "ETA_IBKR_CLIENT_ID=187"

set "PYTHON_EXE=%ETA_ENGINE%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python.exe"

if not exist "%ETA_LOG_DIR%" mkdir "%ETA_LOG_DIR%"
cd /d "%ETA_ENGINE%"

"%PYTHON_EXE%" scripts\jarvis_strategy_supervisor.py ^
    1>> "%ETA_LOG_DIR%\jarvis_strategy_supervisor.stdout.log" ^
    2>> "%ETA_LOG_DIR%\jarvis_strategy_supervisor.stderr.log"

exit /b %ERRORLEVEL%
