@echo off
REM ETA Kaizen Loop -- daily run via Windows Task Scheduler.
REM Cron: daily 06:00 UTC (registered as ETA-Kaizen-Loop).
REM Logs: var/eta_engine/state/kaizen_cron.log
REM Reports: var/eta_engine/state/kaizen_reports/kaizen_<UTC-stamp>.json
cd /d C:\EvolutionaryTradingAlgo
set ETA_ROOT=C:\EvolutionaryTradingAlgo
if not exist C:\EvolutionaryTradingAlgo\var\eta_engine\state mkdir C:\EvolutionaryTradingAlgo\var\eta_engine\state
echo ====================================================== >> C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_cron.log
echo [%date% %time%] kaizen_loop start >> C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_cron.log
python -m eta_engine.scripts.kaizen_loop --apply >> C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_cron.log 2>&1
echo [%date% %time%] kaizen_loop exit=%ERRORLEVEL% >> C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_cron.log
