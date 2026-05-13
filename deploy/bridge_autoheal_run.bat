@echo off
REM bridge_autoheal runner - one-shot self-healing pass.
REM Invoked by the ETA-Bridge-Autoheal scheduled task every 15 minutes.

set PYTHONIOENCODING=utf-8
set PYTHONPATH=C:\EvolutionaryTradingAlgo

if not exist C:\EvolutionaryTradingAlgo\var mkdir C:\EvolutionaryTradingAlgo\var

echo [%date% %time%] bridge_autoheal cycle starting >> C:\EvolutionaryTradingAlgo\var\bridge_autoheal.log
C:\Users\Administrator\.hermes\hermes-agent\.venv\Scripts\python.exe ^
  -m eta_engine.scripts.bridge_autoheal --once ^
  1>> C:\EvolutionaryTradingAlgo\var\bridge_autoheal.log ^
  2>> C:\EvolutionaryTradingAlgo\var\bridge_autoheal.err
echo [%date% %time%] bridge_autoheal cycle exited code=%ERRORLEVEL% >> C:\EvolutionaryTradingAlgo\var\bridge_autoheal.log
