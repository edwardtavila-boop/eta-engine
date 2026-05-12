@echo off
setlocal EnableExtensions

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "SCRIPTS=%ETA_ENGINE%\deploy\scripts"

set "REGISTER_DASHBOARD=%SCRIPTS%\register_dashboard_api_task.ps1"
set "REGISTER_PROXY=%SCRIPTS%\register_proxy8421_bridge_task.ps1"
set "REGISTER_WATCHDOG=%SCRIPTS%\register_dashboard_proxy_watchdog_task.ps1"
set "REGISTER_AUDIT=%SCRIPTS%\register_vps_ops_hardening_audit_task.ps1"

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
if not exist "%REGISTER_AUDIT%" (
    echo Missing VPS ops audit registrar:
    echo   %REGISTER_AUDIT%
    exit /b 3
)

net session >nul 2>&1
if errorlevel 1 (
    echo Requesting Administrator approval to repair ETA dashboard durability.
    echo This registers dashboard self-heal tasks only; it never places, cancels, flattens, or promotes orders.
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

echo === Register read-only VPS ops hardening audit task ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_AUDIT%" -Start
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
