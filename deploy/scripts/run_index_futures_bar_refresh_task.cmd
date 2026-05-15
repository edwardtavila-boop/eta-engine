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

set "RUN_ID=%RANDOM%_%RANDOM%"
set "STDOUT_TMP=%ETA_LOG_DIR%\index_futures_bar_refresh.%RUN_ID%.stdout.tmp.log"
set "STDERR_TMP=%ETA_LOG_DIR%\index_futures_bar_refresh.%RUN_ID%.stderr.tmp.log"
set "LATEST_JSON=%ETA_STATE_DIR%\index_futures_bar_refresh_latest.json"

"%PYTHON_EXE%" "%ETA_ENGINE%\scripts\refresh_index_futures_bars.py" --json ^
    1> "%STDOUT_TMP%" ^
    2> "%STDERR_TMP%"
set "REFRESH_RC=%ERRORLEVEL%"

if exist "%STDOUT_TMP%" (
    copy /y "%STDOUT_TMP%" "%LATEST_JSON%" >nul
    type "%STDOUT_TMP%" >> "%ETA_LOG_DIR%\index_futures_bar_refresh.stdout.log"
    del "%STDOUT_TMP%" 2>nul
)
if exist "%STDERR_TMP%" (
    type "%STDERR_TMP%" >> "%ETA_LOG_DIR%\index_futures_bar_refresh.stderr.log"
    del "%STDERR_TMP%" 2>nul
)
echo %DATE% %TIME% index_futures_bar_refresh exit_code=%REFRESH_RC% >> "%ETA_LOG_DIR%\index_futures_bar_refresh.task.log"

rem This task only refreshes public NQ/MNQ continuous futures bars for replay and dashboard truth.
rem It never places, cancels, flattens, acknowledges, or promotes orders.
exit /b %REFRESH_RC%
