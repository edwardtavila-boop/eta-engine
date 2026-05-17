@echo off
setlocal EnableExtensions

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "SCRIPTS=%ETA_ENGINE%\deploy\scripts"
set "SCRIPT_NAME=repair_dashboard_durability_admin.cmd"

set "REGISTER_DASHBOARD=%SCRIPTS%\register_dashboard_api_task.ps1"
set "REGISTER_PROXY=%SCRIPTS%\register_proxy8421_bridge_task.ps1"
set "REGISTER_WATCHDOG=%SCRIPTS%\register_dashboard_proxy_watchdog_task.ps1"
set "REGISTER_PUBLIC_EDGE=%SCRIPTS%\register_public_edge_route_watchdog_task.ps1"
set "REGISTER_AUDIT=%SCRIPTS%\register_vps_ops_hardening_audit_task.ps1"
set "REGISTER_CRYPTO_REFRESH=%SCRIPTS%\register_crypto_dashboard_refresh_task.ps1"
set "REGISTER_INDEX_FUTURES_REFRESH=%SCRIPTS%\register_index_futures_bar_refresh_task.ps1"
set "REGISTER_BROKER_STATE_REFRESH=%SCRIPTS%\register_broker_state_refresh_task.ps1"
set "REGISTER_SUPERVISOR_BROKER_RECONCILE=%SCRIPTS%\register_supervisor_broker_reconcile_task.ps1"
set "REGISTER_OPERATOR_QUEUE=%SCRIPTS%\register_operator_queue_heartbeat_task.ps1"
set "REGISTER_PAPER_LIVE=%SCRIPTS%\register_paper_live_transition_check_task.ps1"
set "REPAIR_PUBLIC_EDGE_WATCHDOG=%SCRIPTS%\repair_eta_public_edge_route_watchdog_task.ps1"
set "REPAIR_HEALTHCHECK=%SCRIPTS%\repair_eta_healthcheck_task.ps1"
set "REPAIR_FIRM_COMMAND_CENTER_ENV=%SCRIPTS%\repair_firm_command_center_env.ps1"
set "DRY_RUN=0"
set "NO_ELEVATE=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="/DryRun" set "DRY_RUN=1"
if /I "%~1"=="--dry-run" set "DRY_RUN=1"
if /I "%~1"=="/NoElevate" set "NO_ELEVATE=1"
if /I "%~1"=="--no-elevate" set "NO_ELEVATE=1"
if /I "%~1"=="/?" goto usage
if /I "%~1"=="--help" goto usage
shift
goto parse_args

:args_done

if not exist "%ETA_ENGINE%" (
    echo Missing canonical ETA engine path:
    echo   %ETA_ENGINE%
    exit /b 2
)

if not exist "%REGISTER_DASHBOARD%" (
    echo Missing dashboard API registrar:
    echo   %REGISTER_DASHBOARD%
    exit /b 3
)
if not exist "%REGISTER_PROXY%" (
    echo Missing proxy registrar:
    echo   %REGISTER_PROXY%
    exit /b 3
)
if not exist "%REGISTER_WATCHDOG%" (
    echo Missing proxy watchdog registrar:
    echo   %REGISTER_WATCHDOG%
    exit /b 3
)
if not exist "%REGISTER_PUBLIC_EDGE%" (
    echo Missing public edge route watchdog registrar:
    echo   %REGISTER_PUBLIC_EDGE%
    exit /b 3
)
if not exist "%REGISTER_AUDIT%" (
    echo Missing VPS ops audit registrar:
    echo   %REGISTER_AUDIT%
    exit /b 3
)
if not exist "%REGISTER_CRYPTO_REFRESH%" (
    echo Missing crypto dashboard refresh registrar:
    echo   %REGISTER_CRYPTO_REFRESH%
    exit /b 3
)
if not exist "%REGISTER_INDEX_FUTURES_REFRESH%" (
    echo Missing index futures bar refresh registrar:
    echo   %REGISTER_INDEX_FUTURES_REFRESH%
    exit /b 3
)
if not exist "%REGISTER_BROKER_STATE_REFRESH%" (
    echo Missing broker-state refresh registrar:
    echo   %REGISTER_BROKER_STATE_REFRESH%
    exit /b 3
)
if not exist "%REGISTER_SUPERVISOR_BROKER_RECONCILE%" (
    echo Missing supervisor-broker reconcile registrar:
    echo   %REGISTER_SUPERVISOR_BROKER_RECONCILE%
    exit /b 3
)
if not exist "%REGISTER_OPERATOR_QUEUE%" (
    echo Missing operator queue heartbeat registrar:
    echo   %REGISTER_OPERATOR_QUEUE%
    exit /b 3
)
if not exist "%REGISTER_PAPER_LIVE%" (
    echo Missing paper-live transition registrar:
    echo   %REGISTER_PAPER_LIVE%
    exit /b 3
)
if not exist "%REPAIR_PUBLIC_EDGE_WATCHDOG%" (
    echo Missing public edge route watchdog repair script:
    echo   %REPAIR_PUBLIC_EDGE_WATCHDOG%
    exit /b 3
)
if not exist "%REPAIR_HEALTHCHECK%" (
    echo Missing healthcheck repair script:
    echo   %REPAIR_HEALTHCHECK%
    exit /b 3
)
if not exist "%REPAIR_FIRM_COMMAND_CENTER_ENV%" (
    echo Missing FirmCommandCenter env repair script:
    echo   %REPAIR_FIRM_COMMAND_CENTER_ENV%
    exit /b 3
)

if "%DRY_RUN%"=="1" (
    pushd "%ETA_ROOT%" >nul

    echo === DRY RUN: validate ETA dashboard API task registrar ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_DASHBOARD%" -DryRun
    if errorlevel 1 goto fail

    echo === DRY RUN: validate ETA dashboard proxy task registrar ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_PROXY%" -WhatIf
    if errorlevel 1 goto fail

    echo === DRY RUN: validate ETA dashboard proxy watchdog registrar ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_WATCHDOG%" -WhatIf
    if errorlevel 1 goto fail

    echo === DRY RUN: validate ETA public edge route watchdog registrar ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_PUBLIC_EDGE%" -WhatIf
    if errorlevel 1 goto fail

    echo === DRY RUN: validate read-only VPS ops hardening audit registrar ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_AUDIT%" -DryRun
    if errorlevel 1 goto fail

    echo === DRY RUN: validate crypto dashboard bar refresh registrar ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_CRYPTO_REFRESH%" -DryRun
    if errorlevel 1 goto fail

    echo === DRY RUN: validate index futures bar refresh registrar ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_INDEX_FUTURES_REFRESH%" -DryRun
    if errorlevel 1 goto fail

    echo === DRY RUN: validate read-only broker-state refresh registrar ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_BROKER_STATE_REFRESH%" -DryRun
    if errorlevel 1 goto fail

    echo === DRY RUN: validate read-only supervisor-broker reconcile registrar ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_SUPERVISOR_BROKER_RECONCILE%" -DryRun
    if errorlevel 1 goto fail

    echo === DRY RUN: validate read-only operator queue heartbeat registrar ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_OPERATOR_QUEUE%" -DryRun
    if errorlevel 1 goto fail

    echo === DRY RUN: validate read-only paper-live transition cache registrar ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_PAPER_LIVE%" -DryRun
    if errorlevel 1 goto fail

    echo === DRY RUN: validate canonical ETA-Public-Edge-Route-Watchdog repair ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_PUBLIC_EDGE_WATCHDOG%" -DryRun
    if errorlevel 1 goto fail

    echo === DRY RUN: validate canonical ETA-HealthCheck repair ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_HEALTHCHECK%" -DryRun
    if errorlevel 1 goto fail

    echo === DRY RUN: validate canonical FirmCommandCenter env repair ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_FIRM_COMMAND_CENTER_ENV%" -DryRun
    if errorlevel 1 goto fail

    echo DRY RUN OK: dashboard durability repair prerequisites are present.
    echo Re-run without /DryRun from an elevated shell to register and start the tasks, including canonical ETA-HealthCheck repair, ETA-Public-Edge-Route-Watchdog, and FirmCommandCenter env repair.
    echo This covers ETA-Public-Edge-Route-Watchdog, ETA-HealthCheck, and FirmCommandCenter env repair.
    popd >nul
    exit /b 0
)

net session >nul 2>&1
if errorlevel 1 (
    if "%NO_ELEVATE%"=="1" (
        echo Administrator rights are required to register ETA dashboard durability tasks.
        echo No changes were made because /NoElevate was supplied.
        echo Safe preflight: %SCRIPT_NAME% /DryRun /NoElevate
        echo Elevated repair: %SCRIPT_NAME%
        exit /b 5
    )
    echo Requesting Administrator approval to repair ETA dashboard durability.
    echo This registers dashboard self-heal, crypto/index-futures bar refresh, operator queue heartbeat, supervisor-broker reconcile, and paper-live cache tasks only; it never places, cancels, flattens, or promotes orders.
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%ETA_ROOT%' -Verb RunAs -WindowStyle Normal"
    exit /b 0
)

pushd "%ETA_ROOT%" >nul

echo === Register ETA dashboard API task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_DASHBOARD%" -Start
if errorlevel 1 goto fail

echo === Register ETA dashboard proxy task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_PROXY%" -Start
if errorlevel 1 goto fail

echo === Register ETA dashboard proxy watchdog task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_WATCHDOG%" -Start -RestartExistingProcess
if errorlevel 1 goto fail

echo === Register ETA public edge route watchdog task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_PUBLIC_EDGE%" -Start -RestartExistingProcess
if errorlevel 1 goto fail

echo === Register read-only VPS ops hardening audit task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_AUDIT%" -Start
if errorlevel 1 goto fail

echo === Register crypto dashboard bar refresh task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_CRYPTO_REFRESH%" -Start
if errorlevel 1 goto fail

echo === Register index futures bar refresh task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_INDEX_FUTURES_REFRESH%" -Start
if errorlevel 1 goto fail

echo === Register read-only broker-state refresh task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_BROKER_STATE_REFRESH%" -Start
if errorlevel 1 goto fail

echo === Register read-only supervisor-broker reconcile task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_SUPERVISOR_BROKER_RECONCILE%" -Start
if errorlevel 1 goto fail

echo === Register read-only operator queue heartbeat task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_OPERATOR_QUEUE%" -Start
if errorlevel 1 goto fail

echo === Register read-only paper-live transition cache task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_PAPER_LIVE%" -Start
if errorlevel 1 goto fail

echo === Repair canonical ETA-Public-Edge-Route-Watchdog task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_PUBLIC_EDGE_WATCHDOG%"
if errorlevel 1 goto fail

echo === Repair canonical ETA-HealthCheck task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_HEALTHCHECK%"
if errorlevel 1 goto fail

echo === Repair FirmCommandCenter environment ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_FIRM_COMMAND_CENTER_ENV%" -Start
if errorlevel 1 goto fail

echo === Refresh read-only VPS ops hardening audit ===
python.exe -m eta_engine.scripts.vps_ops_hardening_audit --json-out
set "AUDIT_RC=%ERRORLEVEL%"

popd >nul
exit /b %AUDIT_RC%

:fail
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul
echo Dashboard durability repair failed with exit code %EXIT_CODE%.
exit /b %EXIT_CODE%

:usage
echo Usage: %SCRIPT_NAME% [/DryRun] [/NoElevate]
echo.
echo   /DryRun     Validate all dashboard durability registrars without registering tasks.
echo   /NoElevate  Do not request UAC elevation; fail clearly if not already elevated.
echo.
echo This launcher never places, cancels, flattens, or promotes orders.
exit /b 0
