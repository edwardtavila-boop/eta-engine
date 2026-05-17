@echo off
setlocal EnableExtensions

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "SCRIPTS=%ETA_ENGINE%\deploy\scripts"
set "SCRIPT_NAME=repair_eta_scheduler_attention_admin.cmd"
set "REPAIR_PUBLIC_EDGE=%SCRIPTS%\repair_eta_public_edge_route_watchdog_admin.cmd"
set "REPAIR_WEEKLY_SHARPE=%SCRIPTS%\repair_eta_weekly_sharpe_admin.cmd"
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

if not exist "%REPAIR_PUBLIC_EDGE%" (
    echo Missing public edge watchdog admin repair:
    echo   %REPAIR_PUBLIC_EDGE%
    exit /b 3
)

if not exist "%REPAIR_WEEKLY_SHARPE%" (
    echo Missing weekly Sharpe admin repair:
    echo   %REPAIR_WEEKLY_SHARPE%
    exit /b 3
)

if "%DRY_RUN%"=="1" (
    pushd "%ETA_ROOT%" >nul
    echo === DRY RUN: validate ETA scheduler attention repair ===
    call "%REPAIR_PUBLIC_EDGE%" /DryRun /NoElevate
    if errorlevel 1 goto fail
    call "%REPAIR_WEEKLY_SHARPE%" /DryRun /NoElevate
    if errorlevel 1 goto fail
    echo DRY RUN OK: remaining ETA scheduler attention repairs are present.
    echo Re-run without /DryRun from an elevated shell to repair ETA-Public-Edge-Route-Watchdog and ETA-WeeklySharpe.
    popd >nul
    exit /b 0
)

net session >nul 2>&1
if errorlevel 1 (
    if "%NO_ELEVATE%"=="1" (
        echo Administrator rights are required to repair remaining ETA scheduler attention tasks.
        echo No changes were made because /NoElevate was supplied.
        echo Safe preflight: %SCRIPT_NAME% /DryRun /NoElevate
        echo Elevated repair: %SCRIPT_NAME%
        exit /b 5
    )
    echo Requesting Administrator approval to repair remaining ETA scheduler attention tasks.
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%ETA_ROOT%' -Verb RunAs -WindowStyle Normal"
    exit /b 0
)

pushd "%ETA_ROOT%" >nul
call "%REPAIR_PUBLIC_EDGE%"
if errorlevel 1 goto fail
call "%REPAIR_WEEKLY_SHARPE%"
if errorlevel 1 goto fail
popd >nul
exit /b 0

:fail
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul
echo ETA scheduler attention repair failed with exit code %EXIT_CODE%.
exit /b %EXIT_CODE%

:usage
echo Usage: %SCRIPT_NAME% [/DryRun] [/NoElevate]
echo.
echo   /DryRun     Validate the remaining ETA scheduler attention repairs without registering tasks.
echo   /NoElevate  Do not request UAC elevation; fail clearly if not already elevated.
exit /b 0
