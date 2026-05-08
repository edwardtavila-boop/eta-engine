# Trust-Rebuild Plan — 2026-05-07

## What Happened Today

In one session, the fleet went through:

1. **Schema fix** (`ae5cc70`) — `EquitySnapshot.account_equity` rejected
   negative values, collapsing every JARVIS consult once the in-process
   sim ledger went underwater. Allowed negative equity through.

2. **Dispatch-collapse fix** (committed earlier; verified via audit
   `3c9b9ed`) — `signals_confluence_scorecard` was ignoring
   `sub_strategy_kind`, so multiple bots silently shared the same
   signal generator. Several "winning" bots were stealing
   `rsi_mr_mnq`'s signals.

3. **Three retire batches** — 14 bots flagged
   `extras["deactivated"]=True` based on the post-dispatch-fix
   strict-gate audit. Net active fleet 33 → 19.

4. **Two promotions** — `volume_profile_mnq` (`ae5cc70` era) and
   `volume_profile_nq` (`b6fd7fb`) — the only two bots in the
   post-retire fleet to clear the Lopez-de-Prado deflated-Sharpe
   screen.

5. **Multiplier-bug umbrella fix** (`ed7e3cc`) — `get_spec("BTC")`
   returned the CME Bitcoin Futures spec (`point_value=5.0`) but the
   supervisor's BTC bots route through Alpaca SPOT (multiplier 1.0).
   Five subsystems used `spec.point_value` directly:
   `bracket_sizing.py` budget cap, supervisor PnL + R-risk,
   `rigor.py` audit friction, `paper_trade_sim.py`,
   `strategy_creation_harness.py`. All five now route through
   `feeds.instrument_specs.effective_point_value(symbol, route)`.

6. **VPS pin updated** to 12 bots, supervisor restarted, real
   paper-broker fills flowing through `broker_router`.

## What Cannot Be Trusted

| Source | Trust | Why |
|---|---|---|
| Supervisor's in-process `bot.realized_pnl` | **Junk** | 5x amplified for BTC, 50x for ETH, contaminated by dispatch-collapse. Resets on supervisor restart so doesn't persist long-term, but any in-flight numbers from a single supervisor run are unreliable. |
| Strict-gate audit reports prior to `ed7e3cc` | **Suspect** | `rigor.py::friction_per_r` used direct `spec.point_value` so BTC/ETH-bot net_expR was inflated. None of the round-1/2/3 retire decisions need reversing -- the BTC bots were retired for net-NEGATIVE despite the inflation -- but the *exact numbers* in those reports are off. |
| `kaizen_actions.jsonl` historical entries | **Mixed** | The retire actions logged before today reflect real fills, but the elite-scoreboard tier metrics inherited the supervisor's amplified PnL. Going forward (post `ed7e3cc`) kaizen reads honest fills. |

## What CAN Be Trusted

| Source | Trust | Why |
|---|---|---|
| `broker_router_fills.jsonl` | **High** | Real broker fills with real prices. No multiplier amplification -- broker fills are what the broker says happened. |
| IBKR DUQ319869 realized PnL | **High** | $16,680.84 of real paper-broker realized gains. Broker's own ledger. |
| Alpaca paper account | **High** | Same as IBKR. We just flattened orphan crypto positions cleanly. |
| Strict-gate audits AFTER `ed7e3cc` | **High** | Engine corrected. `volume_profile_nq` audit at `222110Z` is the first promotion based on a corrected-engine audit. |
| Daily kaizen on real fills (going forward) | **High** | Reads from broker_router_fills.jsonl. Verdicts on post-fix data are gold. |

## Plan

### Phase A — already done today

- ✅ All known multiplier-bug call sites fixed
- ✅ VPS pin = 12 audit-survivors
- ✅ Daily kaizen task registered as SYSTEM, runs 06:00 UTC
- ✅ Orphan crypto positions flattened
- ✅ `cross_bot_position_tracker` ETH ghost cleared

### Phase B — to be done next (no autonomous changes, operator-driven)

1. **Re-run the strict-gate audit** on the 12-bot pin AFTER `ed7e3cc`.
   The `rigor.py` fix changes friction-per-R for BTC/ETH bots, so
   `volume_profile_btc` numbers may shift. Run:
   ```
   python -m eta_engine.scripts.run_strict_gate_audit
   ```

2. **Compare audit predictions to real fills** for the active 12. Pull
   each bot's `broker_router_fills.jsonl` rows over the last 7 days
   (when sample is large enough). Compute realized expR from those
   fills and compare to the audit's predicted expR_net. Within 20% =
   engine trustworthy. Outside 20% = more bugs to find.

3. **Lock the pin for 30 days**. No new bots, no new retires, no
   parameter tuning unless a kill criterion fires (see below). Let
   real fills accumulate so kaizen can produce a trustworthy verdict.

4. **Kill criteria for emergency action**:
   - Any bot exceeds its `daily_loss_limit_pct` (4% per bot per day) →
     auto-deactivated by supervisor.
   - Fleet daily P&L < -2% of equity → JARVIS halts new entries.
   - Single bot loses 5 trades in a row → kaizen monitors, no auto-action.

5. **Day 30 review**. Re-run kaizen on the 30-day real-fill window.
   Bots that have RETIRE recommendations across 2 consecutive days are
   auto-deactivated by the kaizen sidecar (already wired).

### Phase C — code-level guards (next operator pass)

- Add a CI lint that flags direct `.point_value` access on `get_spec`
  results outside `effective_point_value` -- prevents this bug from
  re-emerging on future code edits.
- Add a smoke test that runs ONE complete trade through the
  WalkForwardEngine and asserts the final PnL matches a hand-computed
  expected value, including multiplier.
- Move `_SPOT_CRYPTO_ROOTS` and `_FUTURES_ROOTS` (currently duplicated
  across `bracket_sizing.py` and `instrument_specs.py`) into a single
  source.

## Open Questions

- **Supervisor `realized_pnl` reset**: in-memory only, not persisted, so
  every restart starts fresh. Acceptable? Or should we wire kaizen-
  style fill-attribution-from-broker so bot.realized_pnl is rebuilt
  from broker_router_fills.jsonl on each restart?
- **Sub-1-lot floor**: bracket_sizing's `paper_futures_floor` lifts
  qty to 1 even when ATR sizing says 0.02. In live mode that would be
  50x over-risk per trade. Need a separate sizing model for live.
- **YM / GC live path**: full YM ($248k notional) and full GC ($470k)
  exceed the $10k per-bot cap on margin. Either lift caps for those
  specifically, switch to MYM/MGC variants, or accept they don't run
  live.

## Bottom Line

The codebase has been hardened against the specific bugs we found
today. The audit engine + supervisor + paper-soak harness all share
one source of truth for `point_value` going forward. Real-broker
fills (the gold-standard data source) were never affected by the
bugs. Going forward, treat backtest numbers as a screening tool only;
make pin/retire/promote decisions on the basis of 30+ days of real
fills aggregated by daily kaizen.

---

## Update 2026-05-08 — Round-4 retire on corrected engine

The first strict-gate audit run on the corrected engine
(`reports/strict_gate_20260508T031716Z.json`) flipped 5 prior pin
members from positive to negative net expR:

| Bot | Pre-fix | Post-fix | Action |
|---|---|---|---|
| volume_profile_btc | net -0.040 | net **-0.139**, sh_def -2.14 | **RETIRED** |
| rsi_mr_mnq | net +0.124, split=True | net **-0.003**, split=False | **RETIRED** |
| gc_sweep_reclaim | net +0.131 | net **-0.179** | **RETIRED** |
| cl_sweep_reclaim | net +0.032 | net **-0.052** | **RETIRED** |
| mes_sweep_reclaim | net +0.120 (n=34) | net **-0.484** (n=5 valid) | **RETIRED** |

**The single strict-gate survivor across all 20 audited bots:**

  `volume_profile_mnq` — Sharpe 1.39, expR_net +0.088, **sh_def +2.86**,
  split-stable, 2916 trades. **L+S** flag (legacy AND strict gates pass).

`volume_profile_nq` is the next-strongest at sh_def +2.08 (just below
strict).

### Today's daily kaizen at 06:00 UTC

Auto-applied 8 RETIRE actions via the 2-run confirmation gate (bots
with prior-day RETIRE recommendations now sidecar-deactivated). All
8 overlap with the registry-level retires from rounds 1-3:
btc_crypto_scalp, eth_sweep_reclaim, eth_perp, eth_compression,
natgas_compression, crude_compression, cross_asset_mnq,
euro_vwap_mr. Belt-and-suspenders deactivation.

### Active fleet (7 bots, all positive net on corrected engine)

| Bot | Sym | Sharpe | expR_net | sh_def | Notes |
|---|---|---:|---:|---:|---|
| volume_profile_mnq | MNQ1 | 1.39 | +0.088 | **+2.86** | **STRICT GATE PASS**, only one in fleet |
| volume_profile_nq | NQ1 | 1.13 | +0.080 | +2.08 | Just below strict |
| mbt_funding_basis | MBT | 3.77 | +0.200 | -0.61 | Split-stable, small sample (n=31) |
| m2k_sweep_reclaim | M2K1 | 4.66 | +0.361 | -0.52 | n=23, sample-bonus Sharpe |
| eur_sweep_reclaim | 6E1 | 3.34 | +0.219 | -0.88 | Split-stable, n=25 |
| mnq_anchor_sweep | MNQ1 | 2.07 | +0.167 | -0.86 | Split-stable, n=68 |
| mnq_futures_sage | MNQ1 | 0.70 | +0.039 | -0.74 | Marginal positive, n=701 |

### Hardening since the original doc

- **`bracket_sizing._paper_floor_enabled()`** — live mode auto-disables
  the sub-1-lot floor so a budget cap can never silently over-trade
  on a live account (was a 50× over-risk surface in paper code).
- **`per_bot_budget_usd` registry override** — high-notional contracts
  (YM/GC/ES) can declare their own bigger cap without lifting the
  default fleet-wide.
- **Missing futures roots** added: YM, MYM, ZF, ZT, M6B, M6A, 6B, 6A,
  6J, M6J. Without these, YM bots silently fell into the "other"
  asset-class default ($100/bot) which killed every entry on the cap.
- **`get_spec` symmetry**: bare-form ("YM") and front-month ("YM1")
  forms now resolve to the same spec via `_strip_front_month_suffix`.
- **`_FUTURES_MAP`** in fetcher: YM and MYM TWS routing added so
  `--symbols YM MYM` no longer fails with "no map".
- **`MYM` instrument spec** added to `_SPECS`.

### Data hydration

- **YM, MYM, M2K, RTY**: each ~120,800 5m bars + ~10,500 1h bars,
  624 days of history (2024-08-21 → 2026-05-08) on the VPS.
- **ES 1h**: 7 years (43k bars) resampled from existing 5m source.
- **MBT/MET 1h**: 564 days resampled from 5m source.

The audit engine now has full historical coverage for every active
bot's primary timeframe.
