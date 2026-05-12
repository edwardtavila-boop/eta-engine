@echo off
REM jarvis_status_server runner - invoked by ETA-Jarvis-Status-Server scheduled task.
REM Listens on 127.0.0.1:8643 as the operator's direct contact point for
REM Hermes + JARVIS. Logs to var/jarvis_status_server.log.

set PYTHONIOENCODING=utf-8
set PYTHONPATH=C:\EvolutionaryTradingAlgo

if not exist C:\EvolutionaryTradingAlgo\var mkdir C:\EvolutionaryTradingAlgo\var

echo ====================================================== >> C:\EvolutionaryTradingAlgo\var\jarvis_status_server.log
echo [%date% %time%] jarvis_status_server starting >> C:\EvolutionaryTradingAlgo\var\jarvis_status_server.log
C:\Users\Administrator\.hermes\hermes-agent\.venv\Scripts\python.exe ^
  -m eta_engine.scripts.jarvis_status_server ^
  1>> C:\EvolutionaryTradingAlgo\var\jarvis_status_server.log ^
  2>> C:\EvolutionaryTradingAlgo\var\jarvis_status_server.err
echo [%date% %time%] jarvis_status_server exited code=%ERRORLEVEL% >> C:\EvolutionaryTradingAlgo\var\jarvis_status_server.log
