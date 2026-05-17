# MNQ Engine ↔ Evolutionary Trading Algo — Integration Bridge

**Date:** 2026-04-16
**Purpose:** Single source of truth linking the mnq_backtest / the_firm /
mnq_bot work (Claude Code workspace) to the Evolutionary Trading Algo roadmap
(Claude Cowork + Claude Code). Both sides read from this file.

> **Historical snapshot note:** This bridge captures a 2026-04-16 integration
> view. Treat its candidate, gauntlet, and promotion phrasing as historical
> context, not as current ETA readiness or launch authority.

---

## 1. What the MNQ engine provides to Evolutionary Trading Algo

The MNQ futures bot is the **P0-P1 Engine tier** of the Evolutionary Trading Algo
funnel. Every Apex phase that depends on "the MNQ engine" pulls its
contract from here, not from inherited prose.

| Apex Phase | What MNQ engine delivers | Where the artifact lives |
|---|---|---|
| P0_SCAFFOLD | Tradovate + NinjaTrader adapter stubs, secret env | `mnq_backtest/src/mnq_backtest/broker/` |
| P1_BRAIN — indicator suite | eta_v3 Pine (ORB, EMA9/21, ATR, ADX, VWAP, Sweep+Reclaim, BB) | `Base/MNQ_Apex_v3_Firm.pine`, `mnq_backtest/pine/` |
| P1_BRAIN — confluence scorer | 15-voice scoring engine (V1-V15) | `Firm/firm_v3/scorer/` (eta_v3_framework), `the_firm_complete/` |
| P1_BRAIN — session filter | Session classifier + RTH/ETH gating | `mnq_backtest/src/mnq_backtest/data/contract.py` |
| P2_FUEL — futures data | Databento 7yr 1m MNQ/ES/NQ/RTY/YM + 1s BBO/trades | `mnq_backtest/.cache/parquet/` ($100.07 billed) |
| P2_FUEL — cleaning | Gap fill, outlier removal, session labeling | `mnq_backtest/src/mnq_backtest/data/cache.py` |
| P3_PROOF — backtester | Event-driven harness, 14-stage orchestrator | `mnq_backtest/scripts/run_all_phases.py` |
| P3_PROOF — walk-forward | Anchored + rolling WF optimizer | `mnq_backtest/src/mnq_backtest/analysis/walk_forward.py` |
| P3_PROOF — Monte Carlo | Null-portfolio MC at N ∈ {61,68,71,100,150,174,200} | `mnq_backtest/runs/phase_i3_null_mc/` |
| P3_PROOF — Deflated Sharpe | Bailey & López de Prado with n_trials accounting | `mnq_backtest/src/mnq_backtest/analysis/dsr.py`, `docs/phase_i2_preregistration/statistical_constraints.md` |
| P3_PROOF — regime validation | 4-state classifier (RISK-ON/OFF/NEUTRAL/CRISIS) | `the_firm_complete/firm/agents/macro/regime.py` |
| P3_PROOF — adversarial sim | Risk-advocate + red-team seats | `the_firm_complete/firm/agents/risk/` |
| P4_SHIELD — dynamic sizing | ATR-based sizing + Apex $5k constraints | `mnq_backtest/src/mnq_backtest/risk/sizing.py` |
| P4_SHIELD — kill switch | Daily loss + max DD kill | `mnq_backtest/src/mnq_backtest/governance/kill_log.py` |
| P5_EXEC — Tradovate adapter | WS order + fill handling | `mnq_backtest/src/mnq_backtest/broker/tradovate_broker.py` |
| P6_FUNNEL — equity monitor | Real-time per-bot equity + baseline | `the_firm_complete/dashboards/equity.py` |
| P7_OPS — circuit breakers | Bar-close gate, regime freeze, correlation brake | `the_firm_complete/firm/governance/` |
| P9_ROLLOUT — MNQ paper | Phase K paper capture | `mnq_backtest/scripts/phase_k_paper_capture.py` |

---

## 2. Current state snapshot (2026-04-16)

### MNQ engine side
- **~72% of Phase H-I complete** (preregistration drafted, governance
  findings resolved, Databento pull landed).
- **Test suite: 749 passing, 0 failed, 6 skipped** (optional deps).
- **Data: 43.5M rows across 66 shards, $100.07 Databento billed**
  (7yr MNQ/ES/NQ/RTY/YM 1m + 1s BBO + trades).

### Cycle 0002 gauntlet results (fresh, 2026-04-16)
| Candidate | Verdict | PF | n |
|---|---|---|---|
| C002-A (session-open fade) | **KILL** | 0.86 | 176 |
| C002-B (xasset momentum) | **SURVIVES (marginal)** | 1.08 | 1,262 |
| C002-C (VIX Donchian 4h) | **KILL** | 0.90 | 354 |

**C002-B was the only live candidate in that gauntlet snapshot** that looked
eligible for future paper-promotion review once DSR cleared with
`n_trials_prior = 22` (see `statistical_constraints.md section 2.1`).

### Evolutionary Trading Algo roadmap side
- **P0_SCAFFOLD: 70% done.** Scaffold + config + 39-section blueprint
  locked; roadmap HTML live; Firm-board crypto-casino engagement pending.
- **P1_BRAIN: 15% done.** Indicator suite (eta_v3 Pine) is
  reusable; confluence scorer and session filter still to scaffold.
- **P2_FUEL: 20% done.** Futures pipeline (Databento → ArcticDB/parquet)
  exists; crypto WS and on-chain feeds pending.
- **P3_PROOF: 0% in Evolutionary Trading Algo, but MNQ engine has much of this.**
  Walk-forward, Monte Carlo, Deflated Sharpe, regime validation already
  in `mnq_backtest/`. They need to be exposed to the Evolutionary Trading Algo
  package tree rather than rebuilt.

---

## 3. Data contract — what crosses the boundary

### MNQ engine → Evolutionary Trading Algo (upstream)
The MNQ engine emits these artifacts on every trading session:

1. **Blotter parquet** — every closed trade with entry/exit/direction/pnl/exit_reason
   - Schema: matches `runs/cycle_0002/C002-*/blotter.parquet`
   - Consumer: Apex `P6_FUNNEL` equity_monitor
2. **Firm decision stream** — V3 envelope JSON per decision
   - Schema: see `trading-dashboard/frontend/src/store/botStore.ts` (`FirmDecision`)
   - Consumer: Apex `P7_OPS` central dashboard
3. **Regime classification** — current regime + vote per agent
   - Schema: `{regime, vote_risk_on, vote_risk_off, confidence, ts}`
   - Consumer: Apex `P3_PROOF` regime_validation, `P10_AI` regime_model
4. **Kill log** — append-only JSONL of spec deaths with reasons
   - Path: `mnq_backtest/runs/kill_log/kill_log.jsonl`
   - Consumer: Apex `P12_POLISH` open_testing (proves the gate actually kills)

### Evolutionary Trading Algo → MNQ engine (downstream)
The Evolutionary Trading Algo side supplies:

1. **Baseline/sweep policy** — $5,500 MNQ baseline, 10% above-baseline
   sweep trigger, 60/30/10 stake/seed/reserve split
   - Source: `eta_engine/config.json` → `funnel.sweep_policy`
   - Consumer: `mnq_backtest/src/mnq_backtest/accounting/sweep.py` (to be built)
2. **Confluence rubric** — 0-10 scoring weights per voice group
   - Source: `eta_engine/config.json` → `confluence_rubric`
   - Consumer: `the_firm_complete/firm/agents/scorer/` (V3 scorer)
3. **Session/news blackout calendar** — FOMC/CPI/PCE/NFP/GDP windows
   - Source: `eta_engine/brain/calendar/news_blackouts.yaml` (to be built)
   - Consumer: `mnq_backtest/src/mnq_backtest/data/contract.py` session gate

---

## 4. What goes into the next batch

Now that C002-A/C are dead and C002-B is marginal, the next batch has
two parallel tracks:

### Track A — promote C002-B or kill it
1. Compute bootstrap PF CI on C002-B blotter (`n=1,262`, 16yr).
2. Compute Deflated Sharpe with `n_trials_prior = 22`.
3. If DSR clears 95%, lock C002-B paper-track criteria in
   `preregistration.md` and hand to `P9_ROLLOUT.mnq_paper`.
4. If DSR fails, add to kill log and close cycle 0002.

### Track B — spin up Evolutionary Trading Algo P1_BRAIN via reuse
1. Export the V3 15-voice scorer from `the_firm_complete/` into an
   Apex-compatible package at `eta_engine/brain/scorer/`.
2. Wire `eta_engine/config.json.confluence_rubric` as the *only*
   source of weights — both repos read from the same file.
3. Port the session classifier (`mnq_backtest/data/contract.py`) into
   `eta_engine/brain/sessions/`.
4. Stand up the news blackout calendar (FOMC/CPI/PCE/NFP/GDP) from the
   economic-calendar feed.

### Track C — basement-level re-test
Already done this batch: 749 passing, 0 failing, 6 skipped (optional
deps). Re-run on any commit that touches:
- `strategy/`, `broker/`, `governance/`, `risk/`, `data/`

---

## 5. Decision rules

- **No candidate promotes to paper without DSR pass** at the current
  `n_trials_prior`. No exceptions.
- **No Evolutionary Trading Algo phase advances past P3 without a surviving MNQ
  candidate** (C002-B or successor). The engine MUST produce real
  positive expectancy before NQ/crypto/perp bots spin up.
- **Both repos read shared config from `eta_engine/config.json`** —
  any param that appears in both codebases has a single source of truth,
  with `mnq_backtest` and `the_firm_complete` treating it as read-only.
- **Kill log is append-only, timestamp-ordered, cross-repo.** When a
  spec dies in either codebase, it gets written here first:
  `mnq_backtest/runs/kill_log/kill_log.jsonl`.

---

## 6. Roadmap state updates (this batch)

Applied to `eta_engine/roadmap_state.json`:
- `P2_FUEL.futures_data`: `in_progress` → **`done`** (Databento 7yr MNQ+confluence pulled)
- `P3_PROOF.backtest_engine`: `pending` → **`in_progress`** (mnq_backtest harness exists)
- `P3_PROOF.walk_forward`: `pending` → **`in_progress`** (walk_forward.py exists, needs formal WF on C002-B)
- `P3_PROOF.monte_carlo`: `pending` → **`done`** (null-portfolio MC 10k sims at N=61..200)
- `P3_PROOF.deflated_sharpe`: `pending` → **`in_progress`** (framework in place, needs run on C002-B)
- `P3_PROOF.regime_validation`: `pending` → **`in_progress`** (regime classifier in firm_v3, needs cross-regime OOS)
- Overall progress: 8% → **~18%**

---

## 7. Next sync cadence

- **Every `run_all_phases.py` run** writes to `docs/dashboard.html` and
  updates `runs/kill_log/kill_log.jsonl`. Cowork reads these on tick.
- **Every Evolutionary Trading Algo roadmap tick** updates `eta_engine/roadmap_state.json`.
  Claude Code reads this before starting any P-level work to avoid duplicate effort.
- **Every session** starts by reading this file. Anything missing here =
  tribal knowledge risk.
