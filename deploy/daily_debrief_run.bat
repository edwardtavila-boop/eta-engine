@echo off
REM daily_debrief runner - end-of-day digest at 21:30 UTC (5:30 PM ET).
REM Invoked by the ETA-Daily-Debrief scheduled task on weekdays.

set PYTHONIOENCODING=utf-8
set PYTHONPATH=C:\EvolutionaryTradingAlgo

if not exist C:\EvolutionaryTradingAlgo\var mkdir C:\EvolutionaryTradingAlgo\var

echo [%date% %time%] daily_debrief starting >> C:\EvolutionaryTradingAlgo\var\daily_debrief.log
C:\Users\Administrator\.hermes\hermes-agent\.venv\Scripts\python.exe ^
  -m eta_engine.scripts.daily_debrief ^
  1>> C:\EvolutionaryTradingAlgo\var\daily_debrief.log ^
  2>> C:\EvolutionaryTradingAlgo\var\daily_debrief.err
echo [%date% %time%] daily_debrief exited code=%ERRORLEVEL% >> C:\EvolutionaryTradingAlgo\var\daily_debrief.log
