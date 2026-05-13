@echo off
REM hermes_evening_journal runner - end-of-day pattern consolidation.
REM Invoked by ETA-Hermes-EveningJournal at 22:30 ET weekdays.

set PYTHONIOENCODING=utf-8
set PYTHONPATH=C:\EvolutionaryTradingAlgo

if not exist C:\EvolutionaryTradingAlgo\var mkdir C:\EvolutionaryTradingAlgo\var

echo [%date% %time%] evening_journal starting >> C:\EvolutionaryTradingAlgo\var\hermes_evening_journal.log
C:\Users\Administrator\.hermes\hermes-agent\.venv\Scripts\python.exe ^
  -m eta_engine.scripts.hermes_evening_journal ^
  1>> C:\EvolutionaryTradingAlgo\var\hermes_evening_journal.log ^
  2>> C:\EvolutionaryTradingAlgo\var\hermes_evening_journal.err
echo [%date% %time%] evening_journal exited code=%ERRORLEVEL% >> C:\EvolutionaryTradingAlgo\var\hermes_evening_journal.log
