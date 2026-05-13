@echo off
REM anomaly_pulse runner - one-shot anomaly Telegram pulse.
REM Invoked by the ETA-Anomaly-Pulse scheduled task every 15 minutes.
REM Replaces the noisy "Watchdog auto-healed" pings with meaningful
REM "bot X has N consecutive losses" alerts. Quiet on calm runs.

set PYTHONIOENCODING=utf-8
set PYTHONPATH=C:\EvolutionaryTradingAlgo

if not exist C:\EvolutionaryTradingAlgo\var mkdir C:\EvolutionaryTradingAlgo\var

echo [%date% %time%] anomaly_pulse cycle starting >> C:\EvolutionaryTradingAlgo\var\anomaly_pulse.log
C:\Users\Administrator\.hermes\hermes-agent\.venv\Scripts\python.exe ^
  -m eta_engine.scripts.anomaly_telegram_pulse ^
  1>> C:\EvolutionaryTradingAlgo\var\anomaly_pulse.log ^
  2>> C:\EvolutionaryTradingAlgo\var\anomaly_pulse.err
echo [%date% %time%] anomaly_pulse cycle exited code=%ERRORLEVEL% >> C:\EvolutionaryTradingAlgo\var\anomaly_pulse.log
