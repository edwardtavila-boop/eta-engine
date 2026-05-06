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
rem ETA_PAPER_LIVE_ALLOWED_SYMBOLS only applies on the direct_ibkr path. With
rem broker_router route, the routing yaml is the source of truth for which
rem (bot, symbol) pairs route where, so this allowlist is effectively unused
rem in the new flow. Kept for back-compat in case route ever flips back.
set "ETA_PAPER_LIVE_ALLOWED_SYMBOLS=MNQ,MNQ1"
rem Operator-acknowledged: a leftover MNQ=1 paper position from a prior session
rem is benign and unrelated to the crypto path. Setting this env var bypasses
rem the reconcile-divergence guard's mtime check on the ack file (which gets
rem invalidated on every supervisor restart). Clear/remove this env once the
rem stale position is closed at IBKR.
set "ETA_RECONCILE_DIVERGENCE_ACK=1"
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
