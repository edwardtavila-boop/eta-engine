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
