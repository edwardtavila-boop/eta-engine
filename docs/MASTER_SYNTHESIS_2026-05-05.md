# Master Synthesis — Pre-Live Hardening Sprint
**Date:** 2026-05-05
**Branch:** `claude/epic-stonebraker-67ab77`
**Trigger:** "Double check them all and give them a supercharge — this is the core of our project, it needs to be super hardened and polished."

---

## What the operator was told vs. what was true

| Surface | Claim (start) | Reality (audited) |
|--------|---------------|-------------------|
| Profitable bots | 12 winners | 8 verified, 9 confirmed losers |
| Cumulative PnL | $516k | Inflated ~30× by replay-summing |
| Trade count | ~20k | Bracket multiplier bug + look-ahead |
| Pre-live readiness | "Ship it" | Wrong-side stops + duplicate bots present |

The dashboard was not lying intentionally — three independent bugs compounded:
1. **Replay-sum**: each VPS restart reloaded all historical fills into the running totals
2. **Multiplier mismatch**: futures point-value → dollar conversion off by the contract multiplier
3. **Duplicate-bot inflation**: three BTC bots with bit-for-bit identical config, each "earning" the same trade

---

## What we built (8 new modules)

### Realistic execution simulation
- `feeds/instrument_specs.py` — per-instrument tick/point-value/commission/slip table; `is_perpetual` flag for funding accounting
- `feeds/realistic_fill_sim.py` — `realistic` / `pessimistic` / `legacy` modes; same-bar straddle resolved probabilistically rather than always-favorable
- `feeds/funding_ledger.py` — crypto perpetual funding cost accounting (15 tests)

### Signal & order safety
- `feeds/signal_validator.py` — fail-closed gate on entry orders: inverted-stop, RR sanity, notional cap (50% equity), degenerate-qty (24 tests)
- `bots/mnq/bot.py:626-660` — validator wired into `on_signal()`; entries only (exits unconditionally proceed to avoid orphaned positions)
- `venues/ibkr_live.py:419` — futures entries REQUIRE `stop_price` + `target_price`, else hard reject; PAXOS crypto exempt (handled separately)
- `backtest/engine.py` `_Open.__post_init__` — universal invariant: stop on wrong side of entry → `ValueError` at construction

### Strategy creation
- `strategies/anchor_sweep_strategy.py` — named PDH/PDL/PMH/PML/ONH/ONL liquidity sweep + reclaim. Cleared all 5 lights of the elite gate; first new strategy promoted via the harness (10 tests)
- `scripts/strategy_creation_harness.py` — 5-light gate (Signal validity / Sample size / OOS profitability / OOS decay / Beats baseline)
- `scripts/strategy_optimizer.py` — walk-forward grid search
- `scripts/fleet_realism_audit.py` — fleet-wide realism scorer

### Registry hardening
- `strategies/per_bot_registry.py`
  - `_config_signature(a)` — hashable tuple of tradeable config (symbol, timeframe, strategy_kind, sub_strategy_extras, scorecard_config)
  - `find_duplicate_active_bots()` — returns `[(symbol, timeframe, [bot_ids…])]` for any active config-collision
  - `validate_registry_no_duplicates(raise_on_duplicate=True)` — fail-closed validator
- `tests/test_registry_no_duplicates.py` — 6 tests covering current-clean / synthetic-duplicate / deactivated-allowed / different-params / different-symbols / raise-mode

### Live-path dedupe guard (the close-the-loop)
- `feeds/mnq_live_supervisor.py:start()` — calls validator with `raise_on_duplicate=True` BEFORE `await self.bot.start()`. On `RuntimeError`, persists `last_event="registry_dedupe_failed:RuntimeError"` and re-raises. (commit `79ef0c1`)
- `scripts/jarvis_strategy_supervisor.py:load_bots()` — same guard at top of bot-loading. (commit `bec9647`)

Both live entry points now refuse to wire the broker if duplicates exist.

### BTC trio differentiation
Three BTC bots were bit-for-bit identical. Rather than leave two deactivated, parameter-differentiated all three so the slots actually explore the space:

| Bot | Variant | level_lookback | rr_target | atr_stop | min_score | Posture |
|-----|---------|---------------|-----------|----------|-----------|---------|
| `btc_hybrid` | baseline | 48 | 3.0 | 2.0 | 2 | Balanced |
| `btc_regime_trend_etf` | TIGHT | 24 | 2.0 | 1.5 | 3 | Higher WR, lower R, fresher pools |
| `btc_sage_daily_etf` | WIDE | 96 | 4.0 | 2.0 | 2 | Bigger swings, more selective |

Both new variants tagged `promotion_status=research_candidate` — they go through the elite gate before earning production status.

---

## Test inventory

| Suite | Tests | Status |
|-------|-------|--------|
| `test_realistic_fill_sim.py` | 26 | ✅ |
| `test_signal_validator.py` | 24 | ✅ |
| `test_funding_ledger.py` | 15 | ✅ |
| `test_anchor_sweep_strategy.py` | 10 | ✅ |
| `test_macro_provider_freshness.py` | 8 | ✅ |
| `test_registry_no_duplicates.py` | 6 | ✅ |
| `test_live_path_validator_and_brackets.py` | 5 (+3 skipped) | ✅ |
| `test_venue_position_cap_qty_fix.py` | 3 | ✅ |
| **Total new** | **97** | **✅** |

Pre-commit hook ran the full pytest sweep before each landing commit (`79ef0c1`, `bec9647`).

---

## Pre-live verification pipeline (now in place)

```
Strategy idea
   ↓
strategy_creation_harness.py  (walk-forward IS/OOS)
   ↓
5-light elite gate
   1. Signal validity (no inverted stops, RR sane)
   2. Sample size  (≥ N trades per OOS window)
   3. OOS profitable (PnL > 0 across windows)
   4. OOS decay <= threshold (no IS overfit)
   5. Beats baseline (vs buy-and-hold or equal-weight)
   ↓
fleet_realism_audit.py  (slippage, funding, commission re-pricing)
   ↓
per_bot_registry.py  (promotion_status: research_candidate → production)
   ↓
validate_registry_no_duplicates(raise=True)  ← guards both supervisors
   ↓
paper-soak (PAPER_SOAK_README.md)
   ↓
live with real capital
```

The guard at the bottom is the **fail-closed barrier**: any duplicate-config bot will refuse to start the supervisor.

---

## Open items for the operator

1. **BTC fleet over-allocation (advisory)** — 10 active BTC bots; fleet correlation analysis would tell whether they're genuinely diversified or just correlated edges in disguise. Not a bug, a sizing question.
2. **OpenCode CLI race conditions** — second AI agent (PID 48240, started 5/4 8am) repeatedly captured my staged files into its own commit messages (e.g. commit `6343731` got the dedupe-guard message but contained TWS watchdog content). Resolved by re-committing the real content as `79ef0c1`. Coordination protocol is the long-term fix.
3. **Push to remote** — local commits `79ef0c1` and `bec9647` have not been pushed; do so before VPS picks up the next pull.
4. **Two new BTC variants need elite-gate runs** — `btc_regime_trend_etf_v2_tight` and `btc_sage_daily_etf_v2_wide` are flagged `research_candidate`; run the harness against them before promoting.

---

## Commit chain (this sprint)

```
bec9647  live-path: extend dedupe guard to JarvisStrategySupervisor.load_bots
79ef0c1  live-path: dedupe guard + BTC differentiation (real)
6343731  live-path: wire registry-dedupe guard at startup + differentiate BTC variants
         (mislabeled by OpenCode race — actually tws_watchdog + dashboard_api)
7b6d7b0  ops: surface broker router execution state
c12421c  kaizen: close the loop -- auto-RETIRE actually deactivates via sidecar override
0a3aa15  docs: append 90d/180d audit synthesis + overnight final summary
869e6bb  ops: separate signals from trade fills
```

---

## What changed that matters most

Before this sprint:
- Dashboard PnL was inflated 30×
- Three BTC bots were the same bot
- Wrong-side stops could ship to the broker
- Futures entries could ship without a bracket
- Same-bar straddles always resolved favorably in backtest

After this sprint:
- Realistic fill sim matches the live venue assumptions
- Validator + bracket-or-reject + `_Open` invariant block malformed orders at three layers
- Dedupe validator + supervisor guards block same-config duplicates from ever reaching the broker
- 97 new tests prove each guard
- Anchor-sweep is the first new strategy through the elite-gate pipeline
- BTC trio explores parameter space rather than triplicating one edge

The pre-live pipeline now has the guards it needed before the first live dollar.
