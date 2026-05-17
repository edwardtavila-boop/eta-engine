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
set "STDOUT_TMP=%ETA_LOG_DIR%\flaw_hardening_heartbeat.%RUN_ID%.stdout.tmp.log"
set "STDERR_TMP=%ETA_LOG_DIR%\flaw_hardening_heartbeat.%RUN_ID%.stderr.tmp.log"

"%PYTHON_EXE%" -m eta_engine.scripts.flaw_hardening_heartbeat ^
    --out "%ETA_STATE_DIR%\flaw_hardening_snapshot.json" ^
    --previous "%ETA_STATE_DIR%\flaw_hardening_snapshot.previous.json" ^
    --changed-only ^
    1> "%STDOUT_TMP%" ^
    2> "%STDERR_TMP%"
set "HEARTBEAT_RC=%ERRORLEVEL%"

if exist "%STDOUT_TMP%" (
    type "%STDOUT_TMP%" >> "%ETA_LOG_DIR%\flaw_hardening_heartbeat.stdout.log"
    del "%STDOUT_TMP%" 2>nul
)
if exist "%STDERR_TMP%" (
    type "%STDERR_TMP%" >> "%ETA_LOG_DIR%\flaw_hardening_heartbeat.stderr.log"
    del "%STDERR_TMP%" 2>nul
)
echo %DATE% %TIME% flaw_hardening_heartbeat exit_code=%HEARTBEAT_RC% >> "%ETA_LOG_DIR%\flaw_hardening_heartbeat.task.log"

rem This task is read-only with respect to brokers/orders. Blocked flaw
rem surfaces are expected until the operator or Codex clears them.
exit /b %HEARTBEAT_RC%
