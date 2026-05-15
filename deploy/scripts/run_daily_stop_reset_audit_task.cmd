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
set "STDOUT_TMP=%ETA_LOG_DIR%\daily_stop_reset_audit.%RUN_ID%.stdout.tmp.log"
set "STDERR_TMP=%ETA_LOG_DIR%\daily_stop_reset_audit.%RUN_ID%.stderr.tmp.log"

"%PYTHON_EXE%" -m eta_engine.scripts.daily_stop_reset_audit ^
    --out "%ETA_STATE_DIR%\daily_stop_reset_audit_latest.json" ^
    --json ^
    1> "%STDOUT_TMP%" ^
    2> "%STDERR_TMP%"

set "AUDIT_RC=%ERRORLEVEL%"
if exist "%STDOUT_TMP%" (
    type "%STDOUT_TMP%" >> "%ETA_LOG_DIR%\daily_stop_reset_audit.stdout.log"
    del "%STDOUT_TMP%" 2>nul
)
if exist "%STDERR_TMP%" (
    type "%STDERR_TMP%" >> "%ETA_LOG_DIR%\daily_stop_reset_audit.stderr.log"
    del "%STDERR_TMP%" 2>nul
)
echo %DATE% %TIME% daily_stop_reset_audit exit_code=%AUDIT_RC% >> "%ETA_LOG_DIR%\daily_stop_reset_audit.task.log"

rem The audit is read-only and may report "held" or "blocked" by design.
rem Keep the Windows task healthy; the JSON artifact is the operator signal.
exit /b 0
