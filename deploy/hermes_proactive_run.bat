@echo off
REM hermes_proactive_investigator runner - cron driver.
REM Bridges anomaly_watcher hits to Hermes's jarvis-anomaly-investigator skill.

set PYTHONIOENCODING=utf-8
set PYTHONPATH=C:\EvolutionaryTradingAlgo

if not exist C:\EvolutionaryTradingAlgo\var mkdir C:\EvolutionaryTradingAlgo\var

echo [%date% %time%] hermes_proactive cycle starting >> C:\EvolutionaryTradingAlgo\var\hermes_proactive.log
C:\Users\Administrator\.hermes\hermes-agent\.venv\Scripts\python.exe ^
  -m eta_engine.scripts.hermes_proactive_investigator ^
  1>> C:\EvolutionaryTradingAlgo\var\hermes_proactive.log ^
  2>> C:\EvolutionaryTradingAlgo\var\hermes_proactive.err
echo [%date% %time%] hermes_proactive cycle exited code=%ERRORLEVEL% >> C:\EvolutionaryTradingAlgo\var\hermes_proactive.log
