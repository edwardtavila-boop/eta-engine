@echo off
REM telegram_inbound runner - long-running Telegram bot listener.
REM Invoked by ETA-Telegram-Inbound scheduled task at boot (AtStartup trigger).
REM
REM Loops forever calling Telegram getUpdates with a 25s long-poll.
REM Whitelisted to TELEGRAM_CHAT_ID for security.

set PYTHONIOENCODING=utf-8
set PYTHONPATH=C:\EvolutionaryTradingAlgo

if not exist C:\EvolutionaryTradingAlgo\var mkdir C:\EvolutionaryTradingAlgo\var

echo [%date% %time%] telegram_inbound starting >> C:\EvolutionaryTradingAlgo\var\telegram_inbound.log
C:\Users\Administrator\.hermes\hermes-agent\.venv\Scripts\python.exe ^
  -m eta_engine.scripts.telegram_inbound_bot ^
  1>> C:\EvolutionaryTradingAlgo\var\telegram_inbound.log ^
  2>> C:\EvolutionaryTradingAlgo\var\telegram_inbound.err
echo [%date% %time%] telegram_inbound exited code=%ERRORLEVEL% >> C:\EvolutionaryTradingAlgo\var\telegram_inbound.log
