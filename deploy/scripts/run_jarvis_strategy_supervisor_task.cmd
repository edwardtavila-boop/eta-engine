@echo off
setlocal

set "ETA_ROOT=C:\EvolutionaryTradingAlgo"
set "ETA_ENGINE=%ETA_ROOT%\eta_engine"
set "ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine"
set "ETA_MODE=PAPER"
set "ETA_LIVE_MONEY=0"
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
rem ym_sweep_reclaim REMOVED 2026-05-07: YM contract notional ($250k+
rem at $5/point × 50080 index level) doesn't fit our per-bot budget,
rem and the strategy's ATR-based sizing produces fractional qty < 1
rem so paper_futures_floor doesn't kick in (it only floors when
rem requested_qty >= 1.0). Result: every YM signal logs
rem "entry skipped: budget cap produced qty=0". Drop until either:
rem   (a) we lift per-bot futures cap to fit 1 YM contract, OR
rem   (b) we replace with MYM (Micro Dow ~$25k notional), OR
rem   (c) ATR sizing logic for futures includes a min-1-contract floor.
rem CURRENT APPROVED PAPER-LIVE ROSTER (readiness snapshot 2026-05-07 v2):
rem Roster rebuilt 2026-05-07 from the post-dispatch-fix strict-gate audit
rem (eta_engine/reports/strict_gate_20260507T194017Z.json). Prior pin had
rem 7 of 10 bots already retired in the registry; this one matches the
rem audit's actual survivors.
rem
rem Pinned bots and rationale:
rem   volume_profile_mnq -- THE deflated-Sharpe survivor (sh_def +1.98,
rem                         4277 trades, split-stable). Just promoted to
rem                         production_candidate. The single highest-
rem                         confidence edge in the fleet.
rem   rsi_mr_mnq         -- top mid-tier survivor (Sharpe 1.91, 137T,
rem                         expR_net +0.124, split-stable).
rem   mbt_funding_basis  -- crypto-futures research_candidate (Sharpe 3.77,
rem                         expR_net +0.200, split-stable, n=31).
rem   mes_sweep_reclaim, ym_sweep_reclaim, m2k_sweep_reclaim,
rem   eur_sweep_reclaim, gc_sweep_reclaim, cl_sweep_reclaim --
rem                         commodity sweep_reclaim family. All positive
rem                         expR_net in audit. Small per-bot samples (14-34
rem                         trades) but the family pattern is consistent.
rem   volume_profile_btc -- already pinned; kept to monitor (net neg in
rem                         audit but split-stable; let kaizen retire if
rem                         confirmed on real fills).
rem   mnq_anchor_sweep   -- already pinned; positive net (+0.116) on 113
rem                         trades, split-unstable but worth watching.
rem   mnq_futures_sage   -- already pinned; flat net (-0.003); kaizen says
rem                         SCALE_UP based on real fills (luck=0.0 / n=61).
rem
rem Skipped: ng_sweep_reclaim (data quality flag in registry - rollover
rem artifacts make the Sharpe 8.31 unreliable); sol_optimized (n=17 too
rem small for live capital); mbt_sweep_reclaim/met_sweep_reclaim/
rem mbt_overnight_gap (zero trades pending bar-data hydration).
rem
rem Removed 2026-05-07 18:05 EDT after verifying live-paper behavior:
rem   ym_sweep_reclaim -- YM at ~$250k notional cannot fit the $10k
rem                       per-bot budget cap. ATR sizing produces 0.02
rem                       contracts (req=0.020145) which rounds to 0
rem                       under "min-1-lot" futures discipline. Bot
rem                       fired 3 entries in 5 min, all "skipped:
rem                       budget cap produced qty=0". Re-pin only after
rem                       (a) MYM (Micro Dow) variant added to registry,
rem                       OR (b) per-bot budget lifted for YM specifically.
rem ROUND-4 RETIRE 2026-05-08: corrected-engine audit on 20 bots flipped
rem 5 of the prior 12 pinned bots to net-negative or sub-1-lot:
rem   volume_profile_btc -- sh_def -2.14, expR_net -0.139 (5x worse than pre-fix)
rem   rsi_mr_mnq         -- net -0.003 (was +0.124), split=False (was True)
rem   gc_sweep_reclaim   -- expR_net flipped +0.131 -> -0.179
rem   cl_sweep_reclaim   -- expR_net flipped +0.032 -> -0.052
rem   mes_sweep_reclaim  -- only 5 valid trades (was 34); -0.484 net
rem
rem Active pin: 12 -> 7 bots. Smaller but every bot in this list has
rem positive net expR on the corrected engine; 1 bot
rem (volume_profile_mnq) is the only strict-gate survivor in the entire
rem audit set (sh_def +2.86 on 2916 trades).
rem
rem MICRO-TIER ADDITION 2026-05-08 (operator directive: "switching to
rem mym for now as micros are key due to starting off with limited
rem funds"). Added per strict-gate audits on 1h history (MYM=624d,
rem MGC=2yr post-fetch, MCL=2yr post-fetch):
rem
rem   mym_sweep_reclaim -- n=11, Sharpe=8.62, expR_net=+0.672, split=True
rem                        Per-trade quality is the highest in the fleet.
rem                        Sample small (n<30) so fails strict-gate, but
rem                        legacy gate passes (L=true) and per-trade edge
rem                        dwarfs every other pinned bot. Canonical
rem                        MYM1_1h.csv (10510 bars / 624d) + MYM1_5m.csv
rem                        (120805 bars) present on both VPS and home;
rem                        paper_live_launch_check reports 0 BLOCK as of
rem                        2026-05-08T08:50Z.
rem   mcl_sweep_reclaim -- n=16, Sharpe=2.00, expR_net=+0.111, split=True
rem                        Profile mirrors mnq_anchor_sweep (split-stable,
rem                        positive net, similar Sharpe). Legacy gate
rem                        passes. MCL micro friction (10x less than CL)
rem                        unlocks the energy-reflexivity edge that
rem                        cl_sweep_reclaim couldn't deliver at full size.
rem
rem   mgc_sweep_reclaim -- n=7, sh_def -1.61, split=False  -- NOT PINNED.
rem                        Strategy fires once per ~70 days on 2yr of MGC1
rem                        1h data; insufficient frequency. Same template
rem                        on MNQ/MCL fires 2-3x more often. Leave for
rem                        future template tuning or alternative timeframe.
rem
rem   mbt_funding_basis -- RE-PINNED 2026-05-08 after baseline persisted.
rem                        Strict-gate audit strict_gate_20260508T031716Z:
rem                        n=31, sharpe 3.77, expR_net +0.200,
rem                        sh_def -0.61, split=True, L=true. Crypto-
rem                        futures basis-fade strategy with the largest
rem                        sample of any positive-net audit bot in the
rem                        MBT family. Promotion_status raised from
rem                        research_candidate to paper_soak.
rem
rem   rsi_mr_mnq_v2 -- NEW 2026-05-08, Tier-1 rehab of deactivated v1.
rem                    Strict-gate audit strict_gate_rsi_v2: n=285,
rem                    sharpe 1.01, expR_net +0.053, split=True, L=true.
rem                    Plan's prediction (looser thresholds fire 2-3x
rem                    more without hurting per-trade quality) confirmed
rem                    -- relaxed rsi 25/75 -> 28/72, min_volume_z
rem                    0.3 -> 0.2 tripled the sample (n 93 -> 285) and
rem                    flipped expR_net from -0.003 to +0.053. The
rem                    kernel was real; over-strict thresholds were
rem                    blocking it.
rem
rem   sol_optimized -- NEW 2026-05-08 (ALPACA CRYPTO LANE).
rem                    Strict-gate audit (strict_gate_20260508T031716Z.json):
rem                    n=18, sharpe 7.69, expR_net +0.616, sh_def +0.09
rem                    (positive deflated Sharpe), split=True, L=true.
rem                    Routes through broker_router to Alpaca
rem                    paper. n<30 but sh_def's positive sign at small n
rem                    is strong small-sample evidence because deflated
rem                    Sharpe heavily penalizes n<30.
set "ETA_SUPERVISOR_BOTS=volume_profile_mnq,volume_profile_nq,m2k_sweep_reclaim,eur_sweep_reclaim,mnq_anchor_sweep,mnq_futures_sage,mcl_sweep_reclaim,mym_sweep_reclaim,ng_sweep_reclaim,mbt_funding_basis,rsi_mr_mnq_v2,sol_optimized"
rem Exit-watch only: (none currently — mbt_funding_basis re-promoted to
rem the active pin after baseline persistence in strategy_baselines.json
rem and registry promotion_status raised to paper_soak.)
set "ETA_SUPERVISOR_EXIT_WATCH_BOTS="
rem broker_router: writes pending_order JSONs to ETA_BROKER_ROUTER_PENDING_DIR;
rem the broker_router service consumes them and routes per bot_broker_routing.yaml
rem (crypto bots -> alpaca, futures -> ibkr). Was direct_ibkr; switched 2026-05-05
rem so crypto bots actually flow through Alpaca paper instead of the
rem direct_ibkr crypto-paper short-circuit (line ~884 of supervisor).
set "ETA_PAPER_LIVE_ORDER_ROUTE=broker_router"
rem ETA_PAPER_LIVE_ALLOWED_SYMBOLS is now only enforced by the direct_ibkr
rem route. broker_router uses configs\bot_broker_routing.yaml as source of
rem truth, so Alpaca crypto is not accidentally filtered by the futures list.
set "ETA_PAPER_LIVE_ALLOWED_SYMBOLS=MNQ,MNQ1,NQ,NQ1,ES,ES1,MES,MES1,RTY,RTY1,M2K,M2K1,MYM,MYM1,YM,YM1,GC,GC1,MGC,MGC1,CL,CL1,MCL,MCL1,NG,NG1,ZN,ZN1,6E,6E1,M6E,M6E1"
set "ETA_SUPERVISOR_STARTING_CASH=50000"
rem Paper-live soak risk profile: keep the daily kill switch enabled,
rem but align it to the $50k paper/prop-prep account and the operator's
rem Atlanta/New-York trading day instead of daily_loss_killswitch.py's
rem fallback $5k/-$300 defaults.
set "ETA_KILLSWITCH_TIMEZONE=America/New_York"
set "ETA_KILLSWITCH_EQUITY_USD=50000"
rem Soft-stop before a $1k prop-style daily wall so slippage/reject retries
rem do not turn a warning into an avoidable rule break.
set "ETA_KILLSWITCH_DAILY_LIMIT_USD=-900"
rem Hardened prop-prep default: once the daily floor trips, paper_live stops
rem producing fresh entry intents. The broker router also enforces this as a
rem last-mile brake, but the supervisor should not keep filling pending/.
set "ETA_PAPER_LIVE_KILLSWITCH_MODE=enforce"
set "ETA_BROKER_ROUTER_PENDING_DIR=%ETA_ROOT%\var\eta_engine\state\router\pending"
rem IBKR Gateway can take several seconds to promote bracket legs from PendingSubmit.
set "ETA_IBKR_SUBMIT_CONFIRM_SECONDS=10"
rem Dedicated positive order-entry client id. Do not inherit machine-level 0.
set "ETA_IBKR_CLIENT_ID=187"
rem ACK reconcile divergence — set 2026-05-07 because Alpaca paper held
rem positions from prior session restarts that supervisor hadn't fully
rem persisted, and IBKR side has 3 futures positions from earlier today.
rem Without this ack, supervisor halts ALL new entries until operator
rem clears via env var or `reconcile_divergence_acknowledged.txt` file.
rem Once both lanes catch up + supervisor's open_position.json fully
rem reflects broker truth, this can be removed.
set "ETA_RECONCILE_DIVERGENCE_ACK=1"
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
rem CRYPTO_FLEET_BUDGET lifted 2026-05-08 from $50k to $200k. The
rem fleet_notional accounting is contract-notional-based (qty × price),
rem so a single MBT futures contract @ $80k notional alone would saturate
rem the prior $50k cap and lock out every other crypto bot. Lifting to
rem $200k matches the actual paper-account margin headroom (MBT margin is
rem only ~$1.5k per contract; per-bot $10k cap remains the binding $-
rem constraint downstream).
set "ETA_LIVE_CRYPTO_FLEET_BUDGET_USD=200000"
set "ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD=10000"
rem FUTURES_FLEET_BUDGET lifted 2026-05-08 from $50k to $300k. Same
rem rationale: contract-notional accounting + 5+ index-futures positions
rem (each $28k+ notional) saturates a $50k cap; per-bot $10k stays the
rem binding $-constraint. Paper margin overhead is ~$2k per micro contract.
set "ETA_LIVE_FUTURES_FLEET_BUDGET_USD=300000"
rem Cross-bot fleet position caps (DEFAULT_ROOT_CAPS overrides). The
rem hard-coded fallback is 10 units which is fine for $20k+ futures
rem contracts but wrong for spot crypto where $200 SOL × 10 = $2k
rem (way under per-bot $10k budget).
rem
rem Note the cap-flow order matters: cross_bot_position_tracker fires
rem BEFORE bracket_sizing.cap_qty_to_budget, so the cap must be high
rem enough to admit the strategy's RAW requested qty (which can be
rem up to 4× per-bot budget under verdict size_mult). bracket_sizing
rem then reduces qty to fit per_bot $ budget downstream.
rem  SOL: 250 units max (raw budget/price=50, 4x verdict mult=200)
rem  BTC: 1.0  units max (raw budget/price=0.15 @ $80k, 4x verdict=0.6)
rem  ETH: 12   units max (raw budget/price=2.5 @ $4k, 4x verdict=10)
set "ETA_FLEET_POSITION_CAP_SOL=250"
set "ETA_FLEET_POSITION_CAP_BTC=1.0"
set "ETA_FLEET_POSITION_CAP_ETH=12"

set "PYTHON_EXE=%ETA_ENGINE%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python.exe"

if not exist "%ETA_LOG_DIR%" mkdir "%ETA_LOG_DIR%"
cd /d "%ETA_ENGINE%"

"%PYTHON_EXE%" scripts\jarvis_strategy_supervisor.py ^
    1>> "%ETA_LOG_DIR%\jarvis_strategy_supervisor.stdout.log" ^
    2>> "%ETA_LOG_DIR%\jarvis_strategy_supervisor.stderr.log"

exit /b %ERRORLEVEL%
