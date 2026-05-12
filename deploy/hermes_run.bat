@echo off
REM Hermes Agent gateway runner — invoked by ETA-Hermes-Agent scheduled task.
REM Sets up env, then runs `hermes gateway run` in foreground.
REM Logs go to C:\EvolutionaryTradingAlgo\var\hermes_gateway.log.

set HERMES_HOME=C:\Users\Administrator\.hermes
set PYTHONIOENCODING=utf-8
set API_SERVER_ENABLED=true
set API_SERVER_PORT=8642
set API_SERVER_HOST=127.0.0.1
REM Load secrets from gitignored sidecar (hermes_secrets.bat). The sidecar
REM is created from hermes_secrets.example.bat with real values. We can't
REM rely on User-scope env-var inheritance because the VPS scheduled task
REM runs as user `trader` but loads Administrator's profile, and the
REM reg-query path silently fails across that split.
if exist "%~dp0hermes_secrets.bat" (
    call "%~dp0hermes_secrets.bat"
) else (
    echo [%date% %time%] ERROR: hermes_secrets.bat missing next to this script >> C:\EvolutionaryTradingAlgo\var\hermes_gateway.log
    echo Copy hermes_secrets.example.bat to hermes_secrets.bat and fill values
    exit /b 1
)

if not exist C:\EvolutionaryTradingAlgo\var mkdir C:\EvolutionaryTradingAlgo\var

echo ====================================================== >> C:\EvolutionaryTradingAlgo\var\hermes_gateway.log
echo [%date% %time%] Hermes gateway starting >> C:\EvolutionaryTradingAlgo\var\hermes_gateway.log
C:\Users\Administrator\.hermes\hermes-agent\.venv\Scripts\python.exe ^
  C:\Users\Administrator\.hermes\hermes-agent\hermes gateway run --accept-hooks ^
  1>> C:\EvolutionaryTradingAlgo\var\hermes_gateway.log ^
  2>> C:\EvolutionaryTradingAlgo\var\hermes_gateway.err
echo [%date% %time%] Hermes gateway exited code=%ERRORLEVEL% >> C:\EvolutionaryTradingAlgo\var\hermes_gateway.log
