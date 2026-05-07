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
set "STDOUT_TMP=%ETA_LOG_DIR%\kaizen_loop.%RUN_ID%.stdout.tmp.log"
set "STDERR_TMP=%ETA_LOG_DIR%\kaizen_loop.%RUN_ID%.stderr.tmp.log"

rem --apply triggers the auto-deactivate path on the 2-run RETIRE
rem confirmation gate; without --apply this is report-only.
"%PYTHON_EXE%" -m eta_engine.scripts.kaizen_loop --apply ^
    1> "%STDOUT_TMP%" ^
    2> "%STDERR_TMP%"

set "KAIZEN_RC=%ERRORLEVEL%"
if exist "%STDOUT_TMP%" (
    type "%STDOUT_TMP%" >> "%ETA_LOG_DIR%\kaizen_loop.stdout.log"
    del "%STDOUT_TMP%" 2>nul
)
if exist "%STDERR_TMP%" (
    type "%STDERR_TMP%" >> "%ETA_LOG_DIR%\kaizen_loop.stderr.log"
    del "%STDERR_TMP%" 2>nul
)
echo %DATE% %TIME% kaizen_loop exit_code=%KAIZEN_RC% >> "%ETA_LOG_DIR%\kaizen_loop.task.log"

rem A non-actionable kaizen run still returns 0 (no bots qualify).
rem Non-zero only on actual error; pass through to scheduler.
exit /b %KAIZEN_RC%
