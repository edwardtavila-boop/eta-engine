@echo off
setlocal

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "ETA_OPS_DIR=%ETA_ROOT%\var\ops"
set "ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine"
set "PYTHONUNBUFFERED=1"

set "PYTHON_EXE=%ETA_ENGINE%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python.exe"

if not exist "%ETA_OPS_DIR%" mkdir "%ETA_OPS_DIR%"
if not exist "%ETA_LOG_DIR%" mkdir "%ETA_LOG_DIR%"
cd /d "%ETA_ROOT%"

set "RUN_ID=%RANDOM%_%RANDOM%"
set "STDOUT_TMP=%ETA_LOG_DIR%\eta_readiness_snapshot.%RUN_ID%.stdout.tmp.log"
set "STDERR_TMP=%ETA_LOG_DIR%\eta_readiness_snapshot.%RUN_ID%.stderr.tmp.log"

powershell.exe -NoProfile -ExecutionPolicy Bypass ^
    -File "%ETA_ROOT%\scripts\eta-readiness-snapshot.ps1" ^
    -Python "%PYTHON_EXE%" ^
    -StatusPath "%ETA_OPS_DIR%\eta_readiness_snapshot_latest.json" ^
    -Json ^
    1> "%STDOUT_TMP%" ^
    2> "%STDERR_TMP%"

set "SNAPSHOT_RC=%ERRORLEVEL%"
if exist "%STDOUT_TMP%" (
    type "%STDOUT_TMP%" >> "%ETA_LOG_DIR%\eta_readiness_snapshot.stdout.log"
    del "%STDOUT_TMP%" 2>nul
)
if exist "%STDERR_TMP%" (
    type "%STDERR_TMP%" >> "%ETA_LOG_DIR%\eta_readiness_snapshot.stderr.log"
    del "%STDERR_TMP%" 2>nul
)
echo %DATE% %TIME% eta_readiness_snapshot exit_code=%SNAPSHOT_RC% >> "%ETA_LOG_DIR%\eta_readiness_snapshot.task.log"

rem BLOCKED is expected during paper soak and returns 0. Nonzero means the
rem snapshot itself failed to refresh, which should show up in Task Scheduler.
exit /b %SNAPSHOT_RC%
