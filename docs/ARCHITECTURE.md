# EVOLUTIONARY TRADING ALGO — Architecture

```
eta_engine/
├── __init__.py                  # Package root
├── pyproject.toml               # Build + deps
├── config.json                  # Single source of truth (all params)
├── roadmap_state.json           # Live progress tracker
├── roadmap_dashboard.html       # Live HTML dashboard (dark terminal)
├── ROADMAP.md                   # 39-section master blueprint
│
├── core/                        # Strategy-agnostic engine
│   ├── risk_engine.py           # Dynamic sizing, Kelly, DD kill, liq-proof
│   ├── confluence_scorer.py     # 0-10 scoring → leverage ramp
│   ├── session_filter.py        # HTF windows + news blackout
│   ├── data_pipeline.py         # Abstract feeds (Bybit, Tradovate)
│   └── sweep_engine.py          # Excess detection + split logic
│
├── bots/                        # 6-bot fleet
│   ├── base_bot.py              # Abstract base (BotConfig, BotState, lifecycle)
│   ├── mnq/bot.py               # ENGINE tier — MNQ futures ($5k start)
│   ├── nq/bot.py                # ENGINE tier — NQ hybrid from MNQ
│   ├── crypto_seed/bot.py       # SEED tier — grid+directional (Bybit)
│   ├── eth_perp/bot.py          # CASINO tier — ETH perps (up to 75x)
│   ├── sol_perp/bot.py          # CASINO tier — SOL perps (up to 75x)
│   └── xrp_perp/bot.py         # CASINO tier — XRP perps (max 50x, thin liq)
│
├── funnel/                      # Profit recycling
│   ├── equity_monitor.py        # Portfolio-wide equity tracker
│   └── transfer.py              # Inter-bot + cold-wallet transfers
│
├── staking/                     # Cold wallet compounding
│   ├── base.py                  # Abstract StakingAdapter
│   ├── lido.py                  # ETH → wstETH (+ EigenLayer opt)
│   ├── jito.py                  # SOL → JitoSOL (MEV-boosted)
│   ├── flare.py                 # XRP → sFLR/FAssets
│   ├── ethena.py                # USDT → sUSDe
│   └── allocator.py             # 40/30/15/15 auto-allocation
│
├── brain/                       # AI layer
│   ├── regime.py                # 5-axis regime classifier
│   ├── rl_agent.py              # PPO/SAC hybrid (stub → train)
│   └── multi_agent.py           # 6-role orchestrator (LLM-MAS-DRL)
│
├── tests/                       # pytest suite
│   ├── conftest.py              # Shared fixtures
│   ├── test_risk_engine.py
│   ├── test_confluence.py
│   ├── test_session_filter.py
│   ├── test_grid.py
│   ├── test_sweep.py
│   └── harness_open.py          # === OPEN FOR FINAL MASTER TWEAKS ===
│
└── docs/
    ├── ARCHITECTURE.md           # This file
    ├── firm_spec_crypto_perp.json # Firm board input spec
    └── kill_log.json              # Board kill/promote decisions
```

## Data Flow

```
Market data (Bybit WS / Tradovate WS)
    │
    ▼
DataFeed.on_bar() / on_tick() / on_l2()
    │
    ▼
Bot.on_bar() → regime_filter() → setup_signals()
    │
    ▼
ConfluenceScorer.score() → 0-10 + leverage recommendation
    │
    ▼
RiskEngine.check() → position size + liq-proof validation
    │
    ▼
SessionFilter.is_ok() → news/window gate
    │
    ▼
Exchange adapter → order placement (Tradovate REST / Bybit v5)
    │
    ▼
Fill → Bot.update_state() → EquityMonitor.update()
    │
    ▼
SweepEngine.check() → excess above baseline?
    │                           │
    ▼ no                        ▼ yes
    (hold)              Transfer.sweep() → split 60/30/10
                            │         │         │
                            ▼         ▼         ▼
                        Staking   Reinvest    Reserve
                        (cold)    (seed bot)   (USDT)
```

## Firm Integration

```
Strategy spec JSON
    │
    ▼
Firm Board: Quant → Red Team → Risk → Macro → Micro → PM
    │
    ▼
Verdict: GO / HOLD / MODIFY / KILL
    │
    ▼
Kill log (docs/kill_log.json) or promotion to next gate
```
