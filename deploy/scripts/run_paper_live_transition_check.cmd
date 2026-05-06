@echo off
setlocal

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "ETA_STATE_DIR=%ETA_ROOT%\var\eta_engine\state"
set "ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine"
set "PYTHONUNBUFFERED=1"

set "PYTHON_EXE=%ETA_ENGINE%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python.exe"

if not exist "%ETA_STATE_DIR%" mkdir "%ETA_STATE_DIR%"
if not exist "%ETA_LOG_DIR%" mkdir "%ETA_LOG_DIR%"
cd /d "%ETA_ROOT%"

"%PYTHON_EXE%" -m eta_engine.scripts.paper_live_transition_check ^
    1>> "%ETA_LOG_DIR%\paper_live_transition_check.stdout.log" ^
    2>> "%ETA_LOG_DIR%\paper_live_transition_check.stderr.log"

set "CHECK_RC=%ERRORLEVEL%"
echo %DATE% %TIME% paper_live_transition_check exit_code=%CHECK_RC% >> "%ETA_LOG_DIR%\paper_live_transition_check.task.log"

rem A blocked paper-live verdict exits nonzero by design. The artifact/logs are
rem the signal; the Windows refresher task should stay healthy while OP-19 is open.
exit /b 0
