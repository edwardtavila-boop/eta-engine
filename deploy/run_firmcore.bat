@echo off
set PYTHONPATH=C:\EvolutionaryTradingAlgo;C:\EvolutionaryTradingAlgo\firm\eta_engine;C:\EvolutionaryTradingAlgo\mnq_bot\src
set ETA_MODE=PAPER
set ETA_SUPERVISOR_MODE=paper_live
set ETA_SUPERVISOR_FEED=composite
set ETA_PAPER_LIVE_ORDER_ROUTE=direct_ibkr
set ETA_PAPER_LIVE_ALLOWED_SYMBOLS=MNQ,MNQ1
set ETA_SUPERVISOR_STARTING_CASH=50000
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set ETA_PYTHON=C:\EvolutionaryTradingAlgo\eta_engine\.venv\Scripts\python.exe
if not exist "%ETA_PYTHON%" set ETA_PYTHON=python
"%ETA_PYTHON%" -m eta_engine.scripts.run_eta_live --live --tick-interval 5
