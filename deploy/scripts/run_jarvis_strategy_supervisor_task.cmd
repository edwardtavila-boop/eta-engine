@echo off
setlocal

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine"
set "ETA_SUPERVISOR_MODE=paper_live"
set "ETA_SUPERVISOR_FEED=composite"
rem Crypto + futures paper-live lane (Alpaca for crypto, IBKR for
rem futures, per configs/bot_broker_routing.yaml).
rem
rem ARCHITECTURAL SPLIT (operator directive 2026-05-06):
rem - ALPACA = SPOT crypto only (BTC/USD, ETH/USD, SOL/USD, etc.)
rem   No funding rates, no perp leverage, no futures concepts.
rem - IBKR = FUTURES + commodities (MNQ, NQ, MES, GC, CL, NG, etc.)
rem   plus crypto futures (MBT/MET CME micros, currently deactivated
rem   pending re-tune for RTH session — see registry).
rem
rem ALPACA SPOT ALPHA SET (4 bots, all spot-native technicals):
rem - btc_optimized: sweep_reclaim + scorecard (DIAMOND #4, 50% WR, +$35k)
rem - vwap_mr_btc: VWAP fade at 2σ (DIAMOND #11, 85.7% paper soak WR)
rem - volume_profile_btc: POC magnetic 168-bar (DIAMOND #13)
rem - eth_sage_daily: sage_gated_orb on ETH (DIAMOND #3, 40% WR, +$3.8k)
rem
rem REMOVED 2026-05-06 — architectural mismatch:
rem - funding_rate_btc: uses funding-rate INPUT (a futures-only concept)
rem   while executing on Alpaca SPOT. Right home is IBKR with MBT
rem   execution once a funding-aware MBT variant is built.
rem
rem PRIOR REMOVAL 2026-05-06 — proven negative-expectancy:
rem - btc_hybrid_sage: 25% WR shadow, losing
rem - btc_ensemble_2of3: 25% WR shadow, losing
rem - crypto_seed: DCA non-edge accumulator
rem
rem FUTURES SET (8 bots): one core per CME/CBOT product (mnq, mes, m2k,
rem ym, gc, cl, ng, zn, eur), routed via broker_router → IBKR.
rem eth_sweep_reclaim was in this list previously but is auto-deactivated
rem by Kaizen daily pass (kaizen_overrides.json: tier=DECAY, mc=DEAD,
rem expectancy_r=-0.0945 on 78 live trades). Removed from the pinned
rem set so the supervisor stops trying to load it on every restart.
rem To re-enable: clear the entry in var/eta_engine/state/kaizen_overrides.json
rem (or run: python -m eta_engine.scripts.kaizen_reactivate eth_sweep_reclaim).
rem KAIZEN SCALE_UP additions 2026-05-06 19:30 UTC — both ELITE/ROBUST
rem with high Sharpe ratios on real live data. Adding to active set
rem per Kaizen's auto-action recommendation:
rem - vwap_mr_mnq: ELITE/ROBUST, Sharpe=6.43, expR=+0.0016 on n=62
rem - mnq_futures_optimized: ELITE/ROBUST, Sharpe=4.18, expR=+0.0011 on n=66
rem Both diversify the futures lane beyond mnq_futures_sage's orb_sage_gated.
set "ETA_SUPERVISOR_BOTS=eth_sage_daily,btc_optimized,vwap_mr_btc,volume_profile_btc,sol_optimized,mnq_futures_sage,vwap_mr_mnq,mnq_futures_optimized,mes_sweep_reclaim,m2k_sweep_reclaim,ym_sweep_reclaim,gc_sweep_reclaim,cl_sweep_reclaim,ng_sweep_reclaim,zn_sweep_reclaim,eur_sweep_reclaim"
rem broker_router: writes pending_order JSONs to ETA_BROKER_ROUTER_PENDING_DIR;
rem the broker_router service consumes them and routes per bot_broker_routing.yaml
rem (crypto bots -> alpaca, futures -> ibkr). Was direct_ibkr; switched 2026-05-05
rem so crypto bots actually flow through Alpaca paper instead of the
rem direct_ibkr crypto-paper short-circuit (line ~884 of supervisor).
set "ETA_PAPER_LIVE_ORDER_ROUTE=broker_router"
rem ETA_PAPER_LIVE_ALLOWED_SYMBOLS is now only enforced by the direct_ibkr
rem route. broker_router uses configs\bot_broker_routing.yaml as source of
rem truth, so Alpaca crypto is not accidentally filtered by the futures list.
set "ETA_PAPER_LIVE_ALLOWED_SYMBOLS=MNQ,MNQ1,NQ,NQ1,ES,ES1,MES,MES1,RTY,RTY1,M2K,M2K1,YM,YM1,GC,GC1,MGC,MGC1,CL,CL1,MCL,MCL1,NG,NG1,ZN,ZN1,6E,6E1,M6E,M6E1"
set "ETA_SUPERVISOR_STARTING_CASH=50000"
set "ETA_BROKER_ROUTER_PENDING_DIR=%ETA_ROOT%\var\eta_engine\state\router\pending"
rem IBKR Gateway can take several seconds to promote bracket legs from PendingSubmit.
set "ETA_IBKR_SUBMIT_CONFIRM_SECONDS=10"
rem Dedicated positive order-entry client id. Do not inherit machine-level 0.
set "ETA_IBKR_CLIENT_ID=187"
rem Capital management — lifted 2026-05-06 to share the FULL $50k
rem starting cash across crypto + futures fleets per operator
rem directive ("crypto fleet should run on the full $50k capital
rem shared with futures and commodities"). The hard daily loss caps
rem (4% per bot in registry) remain the per-bot circuit breaker, and
rem JARVIS verdict size_mult (0.5 APPROVED / 0.24 CONDITIONAL) keeps
rem any single signal from over-deploying.
rem
rem Per-bot caps lifted to $10k = 20% of equity per bot — bots will
rem still self-limit via their max_qty_equity_pct / scorecard logic;
rem this just removes per_bot as the binding constraint so the bot's
rem internal sizing (which has been backtested) can fully express.
rem
rem Fleet caps lifted to full $50k. Crypto is cash-funded so $50k
rem fleet = $50k of coins. Futures uses margin (~$1-2k per micro
rem contract) so $50k notional needs only ~$10-15k of cash margin,
rem leaving room for crypto fleet to coexist on the same Alpaca paper
rem account ($99k equity per dashboard probe).
set "ETA_LIVE_CRYPTO_BUDGET_PER_BOT_USD=10000"
set "ETA_LIVE_CRYPTO_FLEET_BUDGET_USD=50000"
set "ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD=10000"
set "ETA_LIVE_FUTURES_FLEET_BUDGET_USD=50000"

set "PYTHON_EXE=%ETA_ENGINE%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python.exe"

if not exist "%ETA_LOG_DIR%" mkdir "%ETA_LOG_DIR%"
cd /d "%ETA_ENGINE%"

"%PYTHON_EXE%" scripts\jarvis_strategy_supervisor.py ^
    1>> "%ETA_LOG_DIR%\jarvis_strategy_supervisor.stdout.log" ^
    2>> "%ETA_LOG_DIR%\jarvis_strategy_supervisor.stderr.log"

exit /b %ERRORLEVEL%
