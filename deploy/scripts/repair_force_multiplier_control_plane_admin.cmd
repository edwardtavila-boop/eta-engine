@echo off
setlocal EnableExtensions

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "SCRIPT=%ETA_ENGINE%\deploy\scripts\repair_force_multiplier_control_plane.ps1"
set "SELF=%~f0"
set "DRY_RUN=0"
set "NO_ELEVATE=0"
set "START_SERVICE=0"
set "RESTART_SERVICE=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="/DryRun" set "DRY_RUN=1"
if /I "%~1"=="--dry-run" set "DRY_RUN=1"
if /I "%~1"=="/NoElevate" set "NO_ELEVATE=1"
if /I "%~1"=="--no-elevate" set "NO_ELEVATE=1"
if /I "%~1"=="/Start" set "START_SERVICE=1"
if /I "%~1"=="--start" set "START_SERVICE=1"
if /I "%~1"=="/RestartService" set "RESTART_SERVICE=1"
if /I "%~1"=="--restart-service" set "RESTART_SERVICE=1"
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

if not exist "%SCRIPT%" (
    echo Missing Force Multiplier repair script:
    echo   %SCRIPT%
    exit /b 3
)

set "PS_ARGS=-NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%""
if "%DRY_RUN%"=="1" set "PS_ARGS=%PS_ARGS% -DryRun"
if "%START_SERVICE%"=="1" set "PS_ARGS=%PS_ARGS% -Start"
if "%RESTART_SERVICE%"=="1" set "PS_ARGS=%PS_ARGS% -RestartService"

if "%DRY_RUN%"=="1" (
    powershell.exe %PS_ARGS%
    exit /b %ERRORLEVEL%
)

net session >nul 2>&1
if errorlevel 1 (
    if "%NO_ELEVATE%"=="1" (
        echo Administrator rights are required to repair the Force Multiplier control plane.
        echo No changes were made because /NoElevate was supplied.
        echo Safe preflight: repair_force_multiplier_control_plane_admin.cmd /DryRun /NoElevate
        echo Elevated repair: repair_force_multiplier_control_plane_admin.cmd /RestartService
        exit /b 5
    )
    echo Requesting Administrator approval to repair the Force Multiplier control plane.
    echo This repairs FmStatusServer and ETA-ThreeAI-Sync only; it never places, cancels, flattens, or promotes orders.
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%SELF%' -ArgumentList '/RestartService','/NoElevate' -WorkingDirectory '%ETA_ROOT%' -Verb RunAs -WindowStyle Normal"
    exit /b 0
)

powershell.exe %PS_ARGS%
exit /b %ERRORLEVEL%

:usage
echo Usage: repair_force_multiplier_control_plane_admin.cmd [/DryRun] [/NoElevate] [/Start] [/RestartService]
echo.
echo   /DryRun          Validate canonical paths and show the repair plan.
echo   /NoElevate       Do not request UAC elevation; fail clearly if not already elevated.
echo   /Start           Start FmStatusServer after registering service and task.
echo   /RestartService  Restart FmStatusServer and safely stop matching ad-hoc fm_status_server port owners.
echo.
echo This launcher never places, cancels, flattens, or promotes orders.
exit /b 0
