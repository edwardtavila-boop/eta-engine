# Diamond Protection — Truth Surface (2026-05-12)

**Status:** 9 diamond bots locked (8 initial + m2k_sweep_reclaim promoted 2026-05-12). Three-layer protection live. Operator-only
retirement. Falsification criteria pre-committed per bot.

> "Diamonds can only IMPROVE, never disappear" — the runtime gives
> diamonds the benefit of the doubt; the operator retains the kill switch.

---

## The 9 Diamonds

| Bot | Symbol | Tier | Lifetime P&L (paper) | Sessions | Strategy kind |
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

**Total paper P&L:** +$20,866 across the original 8 diamonds. m2k carries
the new R-multiple baseline (+533R / n=1151 / 70% WR) per the canonical
dual-source trade-history archive — the strongest evidence in the fleet.

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

### `mnq_futures_sage` (GOD TIER)
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
python -c "from eta_engine.strategies.per_bot_registry import ASSIGNMENTS, is_active; from eta_engine.feeds.capital_allocator import DIAMOND_BOTS; bad = [a.bot_id for a in ASSIGNMENTS if a.bot_id in DIAMOND_BOTS and not is_active(a)]; print('inactive diamonds:', bad or 'NONE — all 8 active')"

# 3. Kaizen dry-run — verify diamond skip
python -m eta_engine.scripts.kaizen_loop --since 2026-04-01T00:00:00
# Look for "diamond-protected" count in the report

# 4. Allocation snapshot — verify $2k floor
python -m eta_engine.feeds.capital_allocator --print-allocations
```

---

## Cross-references

- `eta_engine/feeds/capital_allocator.py` — `DIAMOND_BOTS` + `DIAMOND_MIN_CAPITAL`
- `eta_engine/scripts/kaizen_loop.py` — `run_loop()` diamond-skip
- `eta_engine/strategies/per_bot_registry.py` — `is_active()` veto layer
- `eta_engine/tests/test_diamond_protection.py` — invariants (created in this commit)
- `var/eta_engine/decisions/{bot_id}_2026_05_12.md` — per-diamond memos
