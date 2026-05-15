@echo off
setlocal

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "ETA_STATE_DIR=%ETA_ROOT%\var\eta_engine\state"
set "ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine"
set "PYTHONUNBUFFERED=1"
set "PWSH_EXE=powershell.exe"

set "PYTHON_EXE=%ETA_ENGINE%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python.exe"

if not exist "%ETA_STATE_DIR%" mkdir "%ETA_STATE_DIR%"
if not exist "%ETA_LOG_DIR%" mkdir "%ETA_LOG_DIR%"
cd /d "%ETA_ROOT%"

set "RUN_ID=%RANDOM%_%RANDOM%"
set "BRACKET_STDOUT_TMP=%ETA_LOG_DIR%\broker_bracket_audit.%RUN_ID%.stdout.tmp.log"
set "BRACKET_STDERR_TMP=%ETA_LOG_DIR%\broker_bracket_audit.%RUN_ID%.stderr.tmp.log"
set "PROMO_STDOUT_TMP=%ETA_LOG_DIR%\prop_strategy_promotion_audit.%RUN_ID%.stdout.tmp.log"
set "PROMO_STDERR_TMP=%ETA_LOG_DIR%\prop_strategy_promotion_audit.%RUN_ID%.stderr.tmp.log"
set "AUDIT_STDOUT_TMP=%ETA_LOG_DIR%\vps_ops_hardening_audit.%RUN_ID%.stdout.tmp.log"
set "AUDIT_STDERR_TMP=%ETA_LOG_DIR%\vps_ops_hardening_audit.%RUN_ID%.stderr.tmp.log"
set "ROOT_REVIEW_STDOUT_TMP=%ETA_LOG_DIR%\root_review_refresh.%RUN_ID%.stdout.tmp.log"
set "ROOT_REVIEW_STDERR_TMP=%ETA_LOG_DIR%\root_review_refresh.%RUN_ID%.stderr.tmp.log"
set "ROOT_REVIEW_INVENTORY=%ETA_STATE_DIR%\vps_root_dirty_inventory.json"
set "ROOT_REVIEW_PLAN=%ETA_STATE_DIR%\vps_root_reconciliation_plan.json"

"%PWSH_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%ETA_ENGINE%\deploy\scripts\inspect_vps_root_dirty.ps1" ^
    -Root "%ETA_ROOT%" ^
    -OutputPath "%ROOT_REVIEW_INVENTORY%" ^
    1> "%ROOT_REVIEW_STDOUT_TMP%" ^
    2> "%ROOT_REVIEW_STDERR_TMP%"
set "ROOT_REVIEW_INSPECT_RC=%ERRORLEVEL%"

if "%ROOT_REVIEW_INSPECT_RC%"=="0" (
    "%PWSH_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%ETA_ENGINE%\deploy\scripts\plan_vps_root_reconciliation.ps1" ^
        -Root "%ETA_ROOT%" ^
        -InventoryPath "%ROOT_REVIEW_INVENTORY%" ^
        -OutputDir "%ETA_STATE_DIR%" ^
        1>> "%ROOT_REVIEW_STDOUT_TMP%" ^
        2>> "%ROOT_REVIEW_STDERR_TMP%"
    set "ROOT_REVIEW_PLAN_RC=%ERRORLEVEL%"
) else (
    set "ROOT_REVIEW_PLAN_RC=9009"
)

if exist "%ROOT_REVIEW_STDOUT_TMP%" (
    type "%ROOT_REVIEW_STDOUT_TMP%" >> "%ETA_LOG_DIR%\root_review_refresh.stdout.log"
    del "%ROOT_REVIEW_STDOUT_TMP%" 2>nul
)
if exist "%ROOT_REVIEW_STDERR_TMP%" (
    type "%ROOT_REVIEW_STDERR_TMP%" >> "%ETA_LOG_DIR%\root_review_refresh.stderr.log"
    del "%ROOT_REVIEW_STDERR_TMP%" 2>nul
)
echo %DATE% %TIME% root_review_refresh inspect_exit_code=%ROOT_REVIEW_INSPECT_RC% plan_exit_code=%ROOT_REVIEW_PLAN_RC% inventory="%ROOT_REVIEW_INVENTORY%" plan="%ROOT_REVIEW_PLAN%" >> "%ETA_LOG_DIR%\vps_ops_hardening_audit.task.log"

"%PYTHON_EXE%" -m eta_engine.scripts.broker_bracket_audit --json ^
    1> "%BRACKET_STDOUT_TMP%" ^
    2> "%BRACKET_STDERR_TMP%"
set "BRACKET_RC=%ERRORLEVEL%"

if exist "%BRACKET_STDOUT_TMP%" (
    type "%BRACKET_STDOUT_TMP%" >> "%ETA_LOG_DIR%\broker_bracket_audit.stdout.log"
    del "%BRACKET_STDOUT_TMP%" 2>nul
)
if exist "%BRACKET_STDERR_TMP%" (
    type "%BRACKET_STDERR_TMP%" >> "%ETA_LOG_DIR%\broker_bracket_audit.stderr.log"
    del "%BRACKET_STDERR_TMP%" 2>nul
)
echo %DATE% %TIME% broker_bracket_audit exit_code=%BRACKET_RC% >> "%ETA_LOG_DIR%\vps_ops_hardening_audit.task.log"

"%PYTHON_EXE%" -m eta_engine.scripts.prop_strategy_promotion_audit --json ^
    1> "%PROMO_STDOUT_TMP%" ^
    2> "%PROMO_STDERR_TMP%"
set "PROMO_RC=%ERRORLEVEL%"

if exist "%PROMO_STDOUT_TMP%" (
    type "%PROMO_STDOUT_TMP%" >> "%ETA_LOG_DIR%\prop_strategy_promotion_audit.stdout.log"
    del "%PROMO_STDOUT_TMP%" 2>nul
)
if exist "%PROMO_STDERR_TMP%" (
    type "%PROMO_STDERR_TMP%" >> "%ETA_LOG_DIR%\prop_strategy_promotion_audit.stderr.log"
    del "%PROMO_STDERR_TMP%" 2>nul
)
echo %DATE% %TIME% prop_strategy_promotion_audit exit_code=%PROMO_RC% >> "%ETA_LOG_DIR%\vps_ops_hardening_audit.task.log"

"%PYTHON_EXE%" -m eta_engine.scripts.vps_ops_hardening_audit --json-out --json ^
    1> "%AUDIT_STDOUT_TMP%" ^
    2> "%AUDIT_STDERR_TMP%"
set "AUDIT_RC=%ERRORLEVEL%"

if exist "%AUDIT_STDOUT_TMP%" (
    type "%AUDIT_STDOUT_TMP%" >> "%ETA_LOG_DIR%\vps_ops_hardening_audit.stdout.log"
    del "%AUDIT_STDOUT_TMP%" 2>nul
)
if exist "%AUDIT_STDERR_TMP%" (
    type "%AUDIT_STDERR_TMP%" >> "%ETA_LOG_DIR%\vps_ops_hardening_audit.stderr.log"
    del "%AUDIT_STDERR_TMP%" 2>nul
)
echo %DATE% %TIME% vps_ops_hardening_audit exit_code=%AUDIT_RC% >> "%ETA_LOG_DIR%\vps_ops_hardening_audit.task.log"

rem Blocked broker/promotion gates are expected during paper soak. The task only
rem fails when the hardening audit itself returns RED_RUNTIME_DEGRADED.
exit /b %AUDIT_RC%
