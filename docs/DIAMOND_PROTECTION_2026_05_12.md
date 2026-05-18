# Diamond Protection — Truth Surface (2026-05-12)

**Status:** 14 diamond bots locked (8 initial + m2k + 5 wave-14 expansion bots, all IBKR-futures-routable). Three-layer protection live. Top 3 by composite score earn PROP_READY designation. Operator-only retirement. Falsification criteria pre-committed per bot.

> **Historical snapshot note:** This memo captures the 2026-05-12 diamond-set
> truth surface. Treat `python -m eta_engine.scripts.prop_launch_check --json`
> and the current leaderboard/readiness artifacts as the live Diamond/Wave-25
> launch authority before acting on older PROP_READY, tier, or promotion labels.

**Wave-16 mandate (2026-05-12):** PROP_READY routing is **IBKR-futures-only**. Alpaca spot is cellared (POOL_SPLIT["spot"]=0.0); Tradovate dormant. Crypto exposure comes from CME micro crypto futures (MET, MBT) routed through IBKR — NOT from BTC/ETH/SOL spot via Alpaca. The `is_ibkr_futures_eligible()` helper in `capital_allocator` enforces this at the leaderboard eligibility layer.

> "Diamonds can only IMPROVE, never disappear" — the runtime gives
> diamonds the benefit of the doubt; the operator retains the kill switch.

---

## Diamond Set Snapshot (2026-05-12)

| Bot | Symbol | Historical tier (2026-05-12) | Lifetime P&L (paper) | Sessions | Strategy kind |
|---|---|---|---|---|---|
| `mnq_futures_sage` | MNQ1 | ROBUST · **GOD TIER** | +$11,246 | 14 | sage_corb (ORB + retest + sage) |
| `nq_futures_sage` | NQ1 | ROBUST | +$2,557 | 7 | sage_corb |
| `cl_momentum` | CL1 | ROBUST | +$2,206 | 13 | commodity_momentum (ROC+ADX+MA) |
| `mcl_sweep_reclaim` | MCL1 | ROBUST | +$2,197 | 13 | sweep_reclaim |
| `mgc_sweep_reclaim` | MGC1 | ROBUST | +$853 | 13 | sweep_reclaim |
| `eur_sweep_reclaim` | 6E1 | **FRAGILE** | +$417 | 13 | sweep_reclaim |
| `gc_momentum` | GC1 | **FRAGILE** | +$142 | 7 | commodity_momentum |
| `cl_macro` | CL1 | confirmed edge | +$1,248 | 7 | oil_macro (2x ATR spike fade) |
| `m2k_sweep_reclaim` | M2K1 | **PROMOTED 2026-05-12** | +533R (n=1151, 70% WR) | — | sweep_reclaim |
| `met_sweep_reclaim` | MET1 | **wave-14 (CRYPTO_FUT)** | +136R (n=208, 69% WR) | — | sweep_reclaim |
| `mes_sweep_reclaim_v2` | MES1 | **wave-14 (FUT_INDEX)** | +136R (n=416, 63% WR) | — | sweep_reclaim |
| `eur_range` | 6E1 | **wave-14 (FX)** | +64R (n=124, 71% WR) | — | range_revert |
| `ng_sweep_reclaim` | NG1 | **wave-14 (COMMODITY_NG)** | +91R (n=243, 65% WR) | — | sweep_reclaim |
| `mes_sweep_reclaim` | MES1 | **wave-14 (FUT_INDEX, paired with v2)** | +56R (n=197, 61% WR) | — | sweep_reclaim |

Historical label note: names such as `GOD TIER`, `PROMOTED 2026-05-12`, and
wave tags in this table are preserved snapshot labels, not current launch
clearance. Use current leaderboard/readiness artifacts plus
`python -m eta_engine.scripts.prop_launch_check --json` for the live verdict.

**Wave-16 demoted (NOT in DIAMOND_BOTS):**
| `volume_profile_btc` | BTC SPOT | **DEMOTED (Alpaca cellared)** | +121R (n=339, 66% WR) | — | volume_profile |

`volume_profile_btc` has strong R-edge (+121R / 66% WR / n=339) but routes through Alpaca SPOT, which the operator has cellared per the IBKR-futures-only mandate. If `POOL_SPLIT["spot"]` is ever reactivated, this bot could return to future Diamond review with a single line in `capital_allocator.DIAMOND_BOTS`, subject to the then-current launch/readiness surfaces.

**Crypto-futures expansion blocker (high-priority kaizen target):**
The MBT family (`mbt_sweep_reclaim`, `mbt_overnight_gap`, `mbt_rth_orb`, `mbt_funding_basis`) trades actively (n=58-129 per bot) but **all `realized_r` values write as 0**. The R-multiple writer is broken for the MBT path. Fixing this writer unlocks 4 candidate Bitcoin-futures diamonds for the IBKR-futures fleet — the natural BTC counterpart to MET. This is the single highest-leverage data-fix in the queue.

**Coverage:** the wave-14 expansion brings the fleet to 15 diamonds
covering all 3 verticals — futures (MES, M2K, MNQ, NQ), commodities
(CL, GC, MGC, MCL, NG), crypto (MET, BTC, MBT-pending). Paper-soak
data accumulation is the primary goal; the top 3 by composite score
(see `diamond_leaderboard.py`) earn PROP_READY designation for the
prop-fund routing layer.

**Total R-edge across the 15 diamonds:** +1,300R+ paper baseline.

---

## Three-Layer Protection

The protection is intentionally redundant so that one source-level mistake
(e.g., someone setting `deactivated: True` on a diamond) cannot silently
kill a proven bot.

### Layer 1 — Capital allocation floor
`eta_engine/feeds/capital_allocator.py`

```python
DIAMOND_BOTS = {"mnq_futures_sage", "nq_futures_sage", "cl_momentum",
                "mcl_sweep_reclaim", "mgc_sweep_reclaim",
                "eur_sweep_reclaim", "gc_momentum", "cl_macro",
                "m2k_sweep_reclaim"}  # promoted 2026-05-12
DIAMOND_MIN_CAPITAL = 2000.0
```

When `compute_allocations()` runs, diamonds receive **at least $2,000** even
if they are unprofitable in the current ledger window. Their `status` stays
`"active"` regardless of P&L.

### Layer 2 — Kaizen auto-RETIRE skip
`eta_engine/scripts/kaizen_loop.py:run_loop()`

The daily Kaizen pass classifies bots as RETIRE / MONITOR / EVOLVE /
SCALE_UP. Before applying any RETIRE, the loop checks `DIAMOND_BOTS`:

```python
if a["bot_id"] in DIAMOND_BOTS:
    a["status"] = "PROTECTED_DIAMOND"
    protected_count += 1
    continue
```

The recommendation is logged as `PROTECTED_DIAMOND` — operator can review,
but no `kaizen_overrides.json` entry is ever written for a diamond.

### Layer 3 — `is_active()` veto
`eta_engine/strategies/per_bot_registry.py:is_active()`

The supervisor's startup gate calls `is_active(assignment)` for every bot.
For diamonds, this function returns `True` **regardless** of:
- `extras["deactivated"] = True` set at the source-code level
- An entry in `kaizen_overrides.json`

This is the strongest layer: the runtime literally refuses to honor a
deactivation marker on a diamond. Removing a diamond requires editing
`DIAMOND_BOTS` in `capital_allocator.py` — a deliberate code change with
a code-review trail.

---

## Correlation matrix — sizing risk

5 of 9 diamonds share underlying instruments (CL/MCL and GC/MGC are
size-different but same-underlying):

| Pair | Correlation expectation | Sizing implication |
|---|---|---|
| `cl_momentum` ↔ `mcl_sweep_reclaim` | High (same underlying, different mechanic) | Cap **combined** notional on CL exposure |
| `cl_momentum` ↔ `cl_macro` | High (both CL, opposite mechanics: momentum vs fade) | Often offsetting → lower net risk, but watch correlation flip |
| `mcl_sweep_reclaim` ↔ `cl_macro` | Medium-high | Same CL exposure layer |
| `mgc_sweep_reclaim` ↔ `gc_momentum` | High (same underlying, different mechanic) | Cap combined GC notional |
| All four CL/MCL bots simultaneous LONG | Worst case | Up to 4× exposure on crude — portfolio limit MUST catch |
| `mnq_futures_sage` ↔ `nq_futures_sage` | Very high (~0.99 — same index, sizing-only difference) | Treat as ONE bet with two sizing knobs |
| `eur_sweep_reclaim` (6E) | Low correlation to everything else | Diversifier — natural portfolio hedge |

**Action required:** `l2_portfolio_limits.py` must enforce a
`max_combined_notional_per_underlying` cap (NOT just `max_concurrent_per_symbol`).
A `MNQ` LONG and an `NQ` LONG should count as the same NASDAQ bet.

---

## Falsification criteria — pre-committed per diamond

These are the operator-only kill triggers. Auto-disable cannot retire a
diamond; only the operator removing it from `DIAMOND_BOTS` after one of
these is hit retires it.

### `mnq_futures_sage` (historical 2026-05-12 GOD TIER label)
- Retire if 30-day rolling P&L < -$5,000 (~half the lifetime gain)
- Retire if 30-day WR < 25% (current peers run 28–35%)
- Retire if 90-day deflated Sharpe < 0
- Review at 60d if 30-day P&L flat (no draw, no gain → mechanic decay)

### `nq_futures_sage`
- Retire if 30-day rolling P&L < -$1,500
- Retire if 30-day n_trades < 5 (signal-cadence cliff)
- Retire if 60-day deflated Sharpe < 0

### `m2k_sweep_reclaim`
- Retire if 30-day rolling P&L < -$800
- Retire if 30-day WR < 45% after at least 50 trades
- Retire if 90-day deflated Sharpe < 0
- Review if the R-multiple edge falls below +0.20R/trade over the next 50 trades

### `cl_momentum`
- Retire if 30-day rolling P&L < -$1,500
- Retire if max drawdown > $1,000 in a single session (volatility spike)
- Retire if regime change: 3-month ADX < 20 across all sessions (no trend → no momentum)

### `mcl_sweep_reclaim`
- Retire if 30-day rolling P&L < -$1,500
- Retire if WR drops below 35% (mechanic requires reclaim → false-sweep regime)
- Retire if 90-day deflated Sharpe < 0

### `mgc_sweep_reclaim`
- Retire if 30-day rolling P&L < -$600 (smaller paper P&L, smaller tolerance)
- Retire if n_trades < 3 in any 30-day window
- Retire if 90-day deflated Sharpe < 0

### `eur_sweep_reclaim` (FRAGILE)
- Retire if 30-day rolling P&L < -$300 (very tight — FRAGILE status acknowledges narrow margin)
- Retire if any 14-day window negative (no recovery cycle)
- Retire if WR < 50%

### `gc_momentum` (FRAGILE)
- Retire if 30-day rolling P&L < -$200 (extremely tight)
- Retire after 5 consecutive losing trades
- Retire if MC verdict ≠ ROBUST after the second monthly evaluation

### `cl_macro` (edge)
- Retire if 30-day rolling P&L < -$1,000
- Retire if "panic spike" days (>2σ ATR) drop below 4 per month — strategy needs the regime
- Retire after 3 consecutive losing trades to verify the edge is stable

---

## Decision-memo discipline

Every diamond gets a memo at `var/eta_engine/decisions/<bot_id>_<date>.md`
with:
1. One-line description
2. Falsification criteria (the table above)
3. Last 30-day backtest snapshot
4. Red Team dissent
5. Operator sign-off line

The 4 L2 shadow strategies already have memos from 2026-05-11. The 9
diamonds get memos in this batch.

---

## Reactivation path (when operator wants to retire a diamond)

There is no auto-reactivation for a retired diamond. The retirement path is:

1. Remove the bot from `DIAMOND_BOTS` in `capital_allocator.py`
2. Set `extras["deactivated"] = True` on the assignment with a written reason
3. Commit + push the change with the deactivation memo cross-referenced
4. Supervisor restart picks up the change on next session

This is **intentionally manual**. The whole point of diamond protection
is that automation cannot kill them; only the operator with explicit
intent + a paper trail.

---

## Verification

Run after any change to the diamond layer:

```powershell
# 1. Code unit tests
python -m pytest eta_engine/tests/test_per_bot_registry.py -q
python -m pytest eta_engine/tests/test_capital_allocator.py -q

# 2. Programmatic check — every diamond reports is_active()=True
python -c "from eta_engine.strategies.per_bot_registry import ASSIGNMENTS, is_active; from eta_engine.feeds.capital_allocator import DIAMOND_BOTS; bad = [a.bot_id for a in ASSIGNMENTS if a.bot_id in DIAMOND_BOTS and not is_active(a)]; print('inactive diamonds:', bad or 'NONE — all 9 active')"

# 3. Kaizen dry-run — verify diamond skip
python -m eta_engine.scripts.kaizen_loop --since 2026-04-01T00:00:00
# Look for "diamond-protected" count in the report

# 4. Allocation snapshot — verify $2k floor
python -m eta_engine.feeds.capital_allocator --print-allocations
```

---

## Promotion gate (wave-6 kaizen, 2026-05-12)

Future promotions must clear a formal gate before being added to
`DIAMOND_BOTS`:

```powershell
python -m eta_engine.scripts.diamond_promotion_gate --include-existing
# Output: var/eta_engine/state/diamond_promotion_gate_latest.json
```

**Hard gates (all 5 must pass — any fail = REJECT):**

| Gate | Threshold | Rationale |
|---|---|---|
| H1_n_trades | n >= 100 | Sample size for stable stats |
| H2_avg_r | avg_r >= +0.20 | Per-trade edge worth burning capital on |
| H3_win_rate | wr >= 45% | Mechanic not entirely lopsided |
| H4_calendar_days | days >= 5 | Regime / day-of-week diversity |
| H5_sessions_positive | sessions+ >= 2 | Not single-session-concentrated |

**Soft gates (all 5 must pass for PROMOTE; any fail = NEEDS_MORE_DATA):**

| Gate | Threshold | Rationale |
|---|---|---|
| S1_n_trades_high | n >= 500 | High-confidence sample |
| S2_avg_r_strong | avg_r >= +0.40 | Clearly-strong edge |
| S3_calendar_days_two_weeks | days >= 14 | Two trading weeks min |
| S4_sessions_breadth | sessions+ >= 3 | Regime breadth |
| S5_no_single_day_dominance | max_day_share < 50% | No single day dominates |

**Verdict semantics:** PROMOTE / NEEDS_MORE_DATA / REJECT.

The gate is **promotion-only** and never demotes. Demotion belongs to
`diamond_falsification_watchdog` (per-bot 30-day USD retirement thresholds).

**Historical note (2026-05-12):** built AFTER the m2k_sweep_reclaim
promotion to formalize the process. Several existing diamonds verdict
REJECT under today's gate (eur_sweep fails H4 with 4 days; mgc_sweep
fails H2 with avg_r=+0.19; mnq/nq sage fail H2 with avg_r≈+0.0007).
They are GRANDFATHERED — never demoted by this gate.

---

## Cross-references

- `eta_engine/feeds/capital_allocator.py` — `DIAMOND_BOTS` + `DIAMOND_MIN_CAPITAL`
- `eta_engine/scripts/kaizen_loop.py` — `run_loop()` diamond-skip
- `eta_engine/scripts/diamond_promotion_gate.py` — formal promotion gate
- `eta_engine/scripts/diamond_falsification_watchdog.py` — retirement thresholds
- `eta_engine/strategies/per_bot_registry.py` — `is_active()` veto layer
- `eta_engine/tests/test_diamond_protection.py` — invariants (created in this commit)
- `eta_engine/tests/test_diamond_promotion_gate.py` — gate semantics
- `var/eta_engine/decisions/{bot_id}_2026_05_12.md` — per-diamond memos
