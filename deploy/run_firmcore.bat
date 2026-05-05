@echo off
set PYTHONPATH=C:\EvolutionaryTradingAlgo;C:\EvolutionaryTradingAlgo\firm\eta_engine;C:\EvolutionaryTradingAlgo\mnq_bot\src
set ETA_MODE=PAPER
set ETA_SUPERVISOR_MODE=paper_live
set ETA_SUPERVISOR_STARTING_CASH=50000
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
C:\EvolutionaryTradingAlgo\eta_engine\.venv\Scripts\python.exe -m eta_engine.scripts.run_eta_live --live --tick-interval 5
