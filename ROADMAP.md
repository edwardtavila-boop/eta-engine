# EVOLUTIONARY TRADING ALGO — Master Roadmap

**Owner:** Edward Avila | **Region:** US-GA | **Start:** 2026-04-16 | **Target completion:** 16 weeks

The full-funnel money-making framework. Four bots → one cold wallet → compounding staking layer.

```
                    ┌─────────────────────┐
                    │   THE FIRM (board)  │  ← Quant / Red Team / Risk / Macro / Micro / PM
                    │   gate every phase  │
                    └──────────┬──────────┘
                               │ verdict
    ┌──────────┬───────────────┼───────────────┬──────────┐
    ▼          ▼               ▼               ▼          ▼
 ┌─────┐   ┌─────┐     ┌──────────────┐   ┌─────────┐  ┌─────────┐
 │ MNQ │→ │ NQ  │ →  │ Crypto Seed  │ → │ ETH/SOL │→│   Cold  │
 │ $5k │   │hybr │     │ grid+direct  │   │  /XRP   │  │  Wallet │
 │tight│   │tight│     │  (profits)   │   │  perps  │  │ staking │
 └─────┘   └─────┘     └──────────────┘   │ CASINO  │  │  layer  │
  engine    engine       turbo-seed       └─────────┘  └─────────┘
                                             turbo         mult
```

---

## Funnel Logic

1. **Engine (MNQ + NQ)** — tight risk, 1-2% per trade, max 5x, liquidation impossible. Excess above baseline (MNQ: $5.5k, NQ: $12k) → sweep.
2. **Turbo-Seed (Crypto Seed)** — grid + directional on Bybit spot/perp. Funded ONLY by swept futures profits. Baseline $2k. Excess → splits to perp bots + cold wallet.
3. **Turbo (ETH/SOL/XRP perps)** — CASINO TIER. Confluence-gated leverage (only 9+ score → up to 75x). Isolated margin. Dynamic sizing keeps liq distance > 3× ATR. Baseline $3k/$3k/$2k. Excess → cold wallet.
4. **Multiplier (Staking)** — Sweep splits 60% cold-stake / 30% reinvest seed / 10% USDT reserve. Stake: 40% wstETH (Lido), 30% JitoSOL, 15% sFLR/XRP, 15% sUSDe. Auto-compound.

---

## Phases (12 phases, 16 weeks)

| Phase | Name | Weeks | Key Deliverable |
|-------|------|-------|-----------------|
| **P0** | Scaffold & Blueprint Lock | 1 | This doc + config.json + package tree + live dashboard |
| **P1** | Strategy & Research Foundation (Brain) | 3 | Top-down engine, confluence scorer 0-10, session filter, casino deltas |
| **P2** | Data Infrastructure (Fuel) | 2 | DataBento futures, Bybit WS + L2, LunarCrush, Blockscout features |
| **P3** | Backtesting/Optimization/Validation (Proof) | 3 | Walk-forward, Monte Carlo, Deflated Sharpe, adversarial sim |
| **P4** | Risk Framework (Shield) | 2 | Dynamic sizing, liq-proof cap, kill switch, Kelly, VaR |
| **P5** | Broker/Exchange Layer (Execution) | 2 | Tradovate, Bybit v5, OKX backup, smart routing, failover |
| **P6** | Profit Funnel & Baseline Automation | 2 | Equity monitor, sweep engine, cold-wallet API sweep |
| **P7** | Execution/Monitoring/Ops | 2 | WS engine, circuit breakers, Grafana, tiered alerts, CI/CD |
| **P8** | Security/Compliance/Legal | 1 | Secret manager, VPS hardening, 2FA, audit trail |
| **P9** | Deployment & Phased Rollout | 4 | Paper → tiny size → scale, per-bot |
| **P10** | AI/ML Layer (RL + Multi-Agent) | 3 | PPO-SAC agent, regime model, LLM-MAS-DRL hybrid, GAN synth |
| **P11** | Staking Compounding Layer | 2 | Lido, Jito, Flare, Ethena adapters + allocator + APY tracker |
| **P12** | Final Optimization & Master Tweaks | 2 | **OPEN TESTING HARNESS** — left open by driver |

*(Phases overlap; total calendar ≈16 weeks not sum of columns.)*

---

## Sections 1-10 (Foundation Blueprint)

### 1. Strategy & Research Foundation (The Brain)
- **Core edge:** every rule documented (entries, exits, filters, HTF bias checks at London/NY open + major news windows). Confluence scoring 0-10 → max leverage only at 9+.
- **Multi-timeframe top-down:** Daily/weekly structure → 4H bias → 5/15-min execution. LTF signals against HTF are rejected.
- **Indicator suite:** current eta_v3 set (ORB, EMA9/21, ATR, ADX, VWAP, Sweep+Reclaim, Bollinger MR) + additions after fine-tuning. Non-redundant, regime-aware.
- **Casino-layer differences:** Futures = 1-2% risk/trade, max 5x. Crypto = higher risk, grid + directional + pyramiding, wider stops — still liquidation-proof via dynamic sizing + isolated margin.
- **Session & macro filters:** hard-coded FOMC/CPI/PCE/NFP/GDP blackouts, volatility-regime gates, order-flow zones.

### 2. Data Infrastructure (The Fuel)
- **Futures:** DataBento tick + 1m via Tradovate WS. Backup: NinjaTrader.
- **Crypto:** Bybit primary — ccxt.pro unified + raw websockets for native liquidation stream. 50-level L2 (snapshot + delta). 60s funding sampling.
- **Storage:** ArcticDB (LMDB) hot + Parquet cold. S3 archives.
- **Cleaning:** gap fill, outlier removal, realistic slippage + commission model.
- **Redundancy:** multiple feeds per asset.
- **New features:** LunarCrush galaxy/sentiment + Blockscout active-address delta + Glassnode onchain.

### 3. Tech Stack
- **Language:** Python 3.14 (match mnq_bot).
- **Core libs:** ccxt.pro, websockets, pandas, polars, numpy, arcticdb, lmdb, pydantic, fastapi, prometheus-client, telegram-bot, solana-py, web3, lido-sdk.
- **ML:** stable-baselines3 (PPO/SAC), torch, scikit-learn, lightgbm, river (online drift).
- **Infra:** Docker, docker-compose, Grafana, Prometheus, Postgres (positions/trades), Redis (state cache).

### 4. Backtesting, Optimization & Validation (The Proof)
- Walk-forward (anchored + rolling OOS).
- Permuted + bootstrapped Monte Carlo — 95% beat requirement.
- Deflated Sharpe + Probabilistic Sharpe adjustment for multiple testing.
- Stress replay: 2008, 2020, 2022 crypto crash, synthetic +50% gap.
- Cross-regime / cross-asset validation (MNQ → NQ/ES, ETH logic → SOL/XRP without re-opt).
- Portfolio-level correlation across all bots.
- Ghost-trader adversarial simulator for stop hunts.
- 6-12 month paper forward-test before real capital.

### 5. Risk Management (Non-Negotiable Core)
- **Dynamic sizing:** ATR/vol-based, never fixed contracts.
- **Liquidation proof:** max_leverage = price / (3×ATR × 1.20 + price × maint_margin_rate). Reject trade if max_leverage < 5.
- **Hard limits:** 2.5% daily loss cap (futures), 6% (crypto casino), 8% max DD kill (futures), 20% (crypto casino).
- **Baseline protection:** per-bot floor; excess flagged for sweep.
- **Fractional Kelly** on crypto layer.
- **Correlation brake:** reduce exposure if cross-bot corr spikes.

### 6. Broker & Exchange Selection
- **Futures:** Tradovate primary (Apex eval compatible), IBKR backup for scale.
- **Crypto perps:** Bybit primary (cleanest v5 API, mature isolated margin, 100x ETH/SOL). Backups: OKX, Bitget.
- **Trade-only API keys.** Withdraw via separate flow with hardware approval.

### 7. Profit Funneling & Baseline Automation (The Funnel)
- **Futures → crypto seed:** broker API equity probe → when > baseline × 1.10 at daily close → alert + semi-auto withdraw to bank → stablecoin on-ramp.
- **Crypto seed → perp bots:** exchange internal transfer once seed > baseline × 1.20.
- **Excess → cold wallet:** Bybit withdraw-to-address API with tightly-scoped key + Ledger Stax hardware approval.
- **Tax-aware logging** per sweep (60/40 futures, short-term crypto, ordinary staking income).

### 8. Execution, Monitoring & Ops
- WS-driven event engine (no polling).
- Circuit breakers: global pause if drawdown > X% or vol > Y%.
- Heartbeat every 30s; auto-shutdown on API failure / extreme vol.
- Grafana + Prometheus + Streamlit for daily reports.
- Tiered alerts: SMS + call (critical), Telegram (normal), email (info).
- Secondary VPS in different DC; hot backup bot.
- Git + CI/CD; automated backtest on every commit.

### 9. Security, Compliance & Legal
- Secrets: env + vault (never in code). Rotate keys.
- VPS hardened (firewall, SSH keys only). 2FA everywhere. Hardware keys on cold storage.
- Georgia US user: CFTC/NFA rules for futures. Crypto perps on compliant venues where possible; document otherwise.
- Per-trade audit trail for taxes (Koinly/CoinTracker/TokenTax export).

### 10. Deployment, Costs & Timeline
- **Phased rollout (≈16 weeks):** strategy+indicators (2-4w) → backtest/MC (3-4w) → MNQ paper (4w) → NQ + funnel → crypto seed → perps → full monitoring → live tiny → scale.
- **Costs:** Data $200-800/mo; VPS $100-300/mo; broker commissions variable; dev time primary.

---

## Sections 11-20 (Institutional-Grade Layers)

### 11. AI/ML Layer — Regime Awareness & Adaptive Edge
- Random Forest / LSTM / Transformer regime classifier on ATR, BBW, orderflow imbalance, funding, VIX-equivalent, macro sentiment.
- 0-10 confluence scorer from HTF bias + indicators + ML probability.
- Anomaly + drift detector — win rate > 2σ below historical → auto-pause or tighten.
- Optional LLM for natural-language strategy logging + idea generation.

### 12. Advanced Validation
- Permuted + bootstrapped MC; strategy must beat 95%+ randomized runs.
- Deflated Sharpe + Probabilistic Sharpe.
- Scenario replay: 2022 crypto crash, 2020 vol spike, 2008 futures moves, +50% gap down.
- Walk-forward anchored + rolling, strict OOS.
- 3-6 month paper min before real capital.

### 13. Execution & Latency
- Co-located VPS: Chicago (CME futures), Singapore/Tokyo (Bybit/OKX).
- WS-only. No HTTP polling.
- Iceberg, post-only, TWAP, conditional orders.
- Hyper-realistic slippage (variable by vol/time).
- Multi-broker auto-failover.

### 14. Enhanced Risk & Liquidation-Proofing
- Dynamic leverage scaler by confluence × vol × distance-to-key-level.
- Isolated margin + auto-deleveraging buffer (crypto).
- Portfolio VaR/CVaR across all bots.
- Optional hedging layer (inverse perp / OTM puts) when DD approaches threshold.
- Funding-rate arbitrage awareness.
- Global circuit breaker.

### 15. Capital Allocation & Funnel Automation
- Kelly / fractional Kelly / vol parity across bots.
- Crypto seed scales only on proven profits.
- Auto-rebalancing + sweeping with multi-sig approval workflow.
- Compounding rules: quarterly review or threshold triggers.

### 16. Monitoring, Ops & Human Oversight
- Central dashboard (Grafana + Streamlit) with equity curves, risk metrics, bot health.
- Comprehensive logging (every decision, signal, trade, state).
- Heartbeat + auto-restart / shutdown.
- Telegram commands: `/status`, `/sweep`, `/pause_crypto`, `/bias_update`.
- Performance attribution by component, session, regime.

### 17. Security & Compliance (2026)
- Zero-trust API keys. HSM if possible.
- Cold/hot wallet split. Multi-sig on cold.
- Backup + disaster recovery (daily snapshots, geo-redundant).
- Insurance (DeFi / exchange hack cover).

### 18. Maintenance & Evolution
- Weekly new-edge review. Quarterly full re-opt.
- Git + auto test pipeline.
- Cost tracking — data, commissions, VPS, slippage. Net edge after costs.
- Designed for scale (add assets/bots).

### 19. Psychological & Longevity
- Full rules-based, audit trail removes emotion.
- Drawdown recovery plan pre-defined.
- KPIs: Sharpe > 1.5 target, max DD < 15-20%, profit factor > 1.5.

### 20. Integration & Phased Roadmap Extras
- Modular monolith first; microservices later.
- Use Freqtrade/Jesse/Hummingbot as crypto grid base to accelerate.
- Data providers: DataBento (futures), Kaiko/CoinAPI/direct Bybit (crypto).

---

## Sections 21-34 (Edge-of-All-Edges — 2026 Cutting Edge)

### 21. Deep RL Core
- PPO + Actor-Critic / Rainbow DQN / SAC with entropy reg.
- Reward = Sharpe-adjusted return − DD penalty − liquidation risk penalty + funding bonus.
- Hybrid safety: hard-coded HTF filters + daily loss caps always gate the RL action.
- Offline pretrain (10y tick) + online finetune.

### 22. Generative AI & Synthetic Data
- GAN / diffusion on historical + real-time → millions of what-if paths (black swans, liquidity crunches, 2022-style crashes).
- Synthetic on-chain data for crypto.

### 23. Multi-Agent Collaborative
- MNQ/NQ = risk-averse elders (set global risk budget).
- Crypto-seed = opportunistic scout.
- ETH/SOL/XRP = high-stakes predators (only deploy on seed excess).
- Shared vector-DB memory + central orchestrator.

### 24. Quantum-Inspired Optimization
- Quantum annealing simulators / VQAs for parameter tuning, allocation, grid/leverage curves.

### 25. Ultra-Realistic Execution & Stress
- Full order-book simulator with adversarial ghost agents that hunt stops.
- Live forward-test harness; new versions must survive 90+ days paper.
- Real-time VaR/CVaR + liquidation-buffer AI.

### 26. Self-Healing & Self-Evolving
- Anomaly + drift detection → auto-pause + re-opt.
- Quarterly evolutionary cycle (live 3mo → retrain RL + resynth → WF validate → deploy).
- Versioned everything, rollback < 60s.

### 27. 2026 Execution Stack
- Futures: Tradovate/IBKR + ib_insync + Chicago colo.
- Crypto: Bybit primary; OKX/Bitget backup; CCXT Pro.
- Infra: Docker + K8s on low-latency VPS; GPU for RL training.
- WS-only, zero HTTP polling.

### 28. Fully Autonomous Profit Funnel
- Real-time equity monitor → internal transfer (futures→crypto) or cold-wallet withdraw.
- Tax-aware logging exports 60/40 + crypto cost basis automatically.
- RL learns optimal withdrawal thresholds.

### 29. Staking Compounding Layer
- Every sweep splits 60% cold-stake / 30% reinvest / 10% USDT reserve.
- ETH: wstETH (Lido) 3.5-3.9% + optional EigenLayer restake 3-8%.
- SOL: JitoSOL 5.8-6.8% (MEV-boosted).
- XRP: Flare sFLR / XRP FAssets 4-8%.
- Stable idle: Ethena sUSDe 7%.
- Custody: Ledger Stax; signing via Rabby.

### 30. Yield Optimizing & Restaking
- Restake ETH via EigenLayer for extra 3-8%.
- SOL: Jito / Kamino lending loops.
- Automated yield aggregator that rotates between safe highest-APY.
- 20-30% stable sleeve for baseline protection.

### 31. Tax & Accounting Automation
- Per-sweep tagging: futures 60/40, crypto short-term, staking ordinary.
- Auto CSV → Koinly / CoinTracker / TokenTax.
- Deferral: hold staking rewards in wallet; IRA/401k crypto exploration.

### 32. Portfolio Hedging & Insurance
- Cheap tail OTM puts on NQ/ETH from excess only.
- DeFi insurance (Nexus Mutual) on staking/restaking.
- RL correlation brake.

### 33. Human-in-the-Loop
- Telegram commands.
- Weekly PDF report (equity, staking growth, swept, projections).
- Tiered alert tuning.

### 34. Capital Growth Feedback Loops
- After X months compounded staking, optional % recycles to seed bot (with approval).
- Baseline evolves 2-4% annually (inflation + staking avg).

---

## Sections 35-39 (Final Expansion)

### 35. Real-Time 2026 Staking Yields (mid-April)
- **ETH:** 3.2-4.2% APY base (Lido/Rocket Pool 3.5-3.9% liquid; solo/MEV 4-5%). EigenLayer restake +3-8%.
- **SOL:** 5.5-7.2% APY (Jito/Helius/Marinade 5.8-6.8%; top validators near 7%).
- **XRP:** 4-8% yield on platform-based / Flare sFLR.
- **Stable:** 4-9% (Aave v3, Spark, Ethena sUSDe).

### 36. LLM-MAS-DRL Hybrid
- Specialized agents: sentiment/news analyzer, regime detector, risk guardian, execution optimizer, staking allocator, supervisor.
- LLM layer for semantic news parsing + natural language strategy updates.
- DRL (PPO/SAC) for action decisions on synthetic + real data.
- MNQ/NQ agents conservative; crypto agents more freedom on house money.

### 37. Execution & Platform Stack (Practical)
- Futures: NinjaTrader/IBKR (tight spreads, Python API).
- Crypto: Phemex/Bybit/OKX (superior risk tools, grid bots, isolated margin).
- Bot bases: Freqtrade / Hummingbot / Jesse for crypto; custom Python for futures/RL.

### 38. Hidden Operational & Longevity
- Synthetic data + adversarial testing (ghost traders).
- Cost/slippage optimizer — adjusts if commissions eat > 20% of edge.
- Exit/succession planning.
- Regulatory evolution watch (automated CFTC/IRS alerts).
- Hardware/offline resilience (air-gapped configs, "safe mode" staking-only if connectivity drops).
- Psychological dashboard (compounding projections, "wealth velocity" score).

### 39. Complete Funnel — Final Mental Model
- **Engine:** MNQ $5k + NQ → tight risk, top-down, baseline protection → excess sweep.
- **Turbo:** Crypto seed + ETH/SOL/XRP perps → house money only, RL-optimized, same baseline + excess logic.
- **Multiplier:** Staking/restaking → 40-60% of excess into liquid ETH/SOL (restaked where safe) + stable sleeve → auto-compound.
- **Brain:** Multi-agent RL + LLM orchestrator.
- **Shield:** Liquidation-proof math, monitoring, hedges, tax logging, redundancy.

---

## Firm Integration

Every promotion gate runs through The Firm (6-agent board). Path:
1. **Spec approve** — Quant generates → Red Team critiques → Risk sets rails → Macro/Micro consult → PM verdict.
2. **Backtest approve** — Performance Analyst scores (Deflated Sharpe, regime slicing, cost attribution).
3. **Paper approve** — 3-6mo paper forward with adversarial stress.
4. **Live approve** — tiny size with circuit breakers, then scale.

Kill log lives at `eta_engine/docs/kill_log.json`. No bot goes live without PM signoff.

---

## The Open Testing Harness (P12)

Left deliberately open by the driver for final optimization + master tweaks after the build is end-to-end functional. Includes:

- `tests/harness_open.py` — parameter-sweep skeleton, forward-test comparator, regime-slice evaluator.
- Hooks for user to inject final tweaks post-build.
- Last mile: user reviews live forward-test output and approves live scaling.

---

*This roadmap is the living blueprint. See `roadmap_state.json` for live progress.*
