@echo off
setlocal EnableExtensions

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "SCRIPTS=%ETA_ENGINE%\deploy\scripts"
set "SCRIPT_NAME=repair_firm_command_center_env_admin.cmd"
set "REPAIR_PS1=%SCRIPTS%\repair_firm_command_center_env.ps1"
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

if not exist "%REPAIR_PS1%" (
    echo Missing FirmCommandCenter env repair script:
    echo   %REPAIR_PS1%
    exit /b 3
)

if "%DRY_RUN%"=="1" (
    pushd "%ETA_ROOT%" >nul
    echo === DRY RUN: validate canonical FirmCommandCenter env repair ===
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_PS1%" -DryRun
    set "EXIT_CODE=%ERRORLEVEL%"
    popd >nul
    exit /b %EXIT_CODE%
)

net session >nul 2>&1
if errorlevel 1 (
    if "%NO_ELEVATE%"=="1" (
        echo Administrator rights are required to repair FirmCommandCenter.
        echo No changes were made because /NoElevate was supplied.
        echo Safe preflight: %SCRIPT_NAME% /DryRun /NoElevate
        echo Elevated repair: %SCRIPT_NAME%
        exit /b 5
    )
    echo Requesting Administrator approval to repair FirmCommandCenter.
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%ETA_ROOT%' -Verb RunAs -WindowStyle Normal"
    exit /b 0
)

pushd "%ETA_ROOT%" >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_PS1%" -Start
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul
exit /b %EXIT_CODE%

:usage
echo Usage: %SCRIPT_NAME% [/DryRun] [/NoElevate]
echo.
echo   /DryRun     Validate the canonical FirmCommandCenter env repair without mutating the venv.
echo   /NoElevate  Do not request UAC elevation; fail clearly if not already elevated.
exit /b 0
