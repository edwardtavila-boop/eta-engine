# Strict-Gate Audit — Post-Round-2 Retire — 2026-05-07 19:40 UTC

**Fleet:** 20 active bots (down from 33 → 29 after round-1 retire → 20 after round-2 retire)
**Run:** `python -m eta_engine.scripts.run_strict_gate_audit`
**Raw data:** `eta_engine/reports/strict_gate_20260507T194017Z.json`

## Headline

| Metric | Pre-fix audit | Round-1 (33→29) | Round-2 (29→20) |
|---|---|---|---|
| Bots evaluated | 33 | 33 | 20 |
| Legacy gate pass | 5 | 11 | 11 |
| Strict gate pass | 0 | 0 | 0 |
| Bots with sh_def > 0 | several (bug-inflated) | 1 | 1 |

The dispatch-collapse fix (3c9b9ed) revealed which "winners" had been borrowing
other bots' signals. The two retire batches removed every bot with negative
expR_net AND structural instability AND/OR enough sample to rule out edge.

## The One Real Signal

**`volume_profile_mnq`** — only bot in the audit with positive deflated Sharpe.

```
trd=4277  sh=0.94  expR_net=+0.050  sh_def=+1.98  split=True  L_
```

- 4277 trades is a meaningful sample (not a tail draw).
- Lopez-de-Prado deflated Sharpe at +1.98 corrects for multiple-comparison
  pressure across the fleet — this is the rarest possible signal in a strict
  audit.
- Split-half sign-stable: edge persists across both halves of the data.
- Legacy gate passes; strict gate fails purely because of the global
  Bonferroni correction (×20 across the audit set).

Operator decision pending: **promote `volume_profile_mnq` to scale-up**?

## Mid-Tier Candidates (positive expR_net, split-stable, small samples)

| Bot | trades | Sharpe | expR_net | sh_def |
|---|---|---|---|---|
| sol_optimized | 17 | 4.91 | +0.398 | -0.65 |
| ym_sweep_reclaim | 23 | 4.66 | +0.379 | -0.52 |
| mbt_funding_basis | 31 | 3.77 | +0.200 | -0.61 |
| rsi_mr_mnq | 137 | 1.91 | +0.124 | -0.51 |
| mes_sweep_reclaim | 34 | 3.97 | +0.120 | -0.47 |

These all show positive net expectancy and split-stability, but trade counts
are too small for the deflated Sharpe to clear zero. Watch with paper-soak;
do not scale up yet.

## Single-Window Outliers

`ng_sweep_reclaim` shows Sharpe 8.31 on 20 trades — the top headline number,
but the registry already flags the bar data has rollover artifacts and the
elite-gate result was "unreproducible on canonical bar files" (`demoted_on:
2026-05-07`). Treat the high Sharpe as data-quality noise. Re-run on
rollover-adjusted NG bars before any promotion.

## Round-3 Borderline Cases (no action this batch)

| Bot | trades | expR_net | split | Why-keep / Why-retire |
|---|---|---|---|---|
| volume_profile_btc | 699 | -0.040 | True | net-neg + sh_def -2.19, but split-stable; **leave** — ambiguous |
| mbt_rth_orb | 144 | -0.016 | True | gross-positive Sharpe (1.22), commission-eroded; **leave** — fixable via param tune |
| mnq_futures_sage | 1156 | -0.003 | True | flat across 1156 trades; **leave** — not actively losing |

The "net negative + split-stable" combo is structurally different from the
round-1/round-2 retires (which were "net negative + unstable + high sh_def").
A reproducible nothing-burger is not the same as confirmed noise; these
bots may be tuned into edge or quietly retired by kaizen on data drift.

## Empty-Sample Bots

`mbt_sweep_reclaim`, `met_sweep_reclaim`, `mbt_overnight_gap` — all show
zero trades. Need bar-data hydration on MBT/MET 5m before they can be
evaluated. Not a retire decision.

## Architectural Observations

- **All BTC architectures are now retired except `volume_profile_btc`** —
  sweep_reclaim, vwap_reversion, funding_rate, ensemble voting, hybrid_sage,
  crypto_scalp, the DCA accumulator. The dispatch-fix audit collapsed every
  one of them.
- **VWAP-reversion family is dead** — vwap_mr_mnq/nq/btc all retired. The
  apparent edge was the dispatch-collapse bug.
- **Cross-asset divergence family is dead** — cross_asset_mnq retired,
  cross_asset_btc was already deactivated.
- **Sweep_reclaim is the surviving alpha pool** — but only at small sample
  sizes (14-34 trades each) on the commodity bots. Aggregate edge across
  the family looks real; individual bots need more trades.

## Operator Action Queue

1. **Decide on `volume_profile_mnq` promotion** — it is the only bot to
   pass deflated-Sharpe screening. SCALE_UP is the kaizen action this
   would map to (auto_apply_safe=False; capital allocation).
2. **Restart `ETAJarvisSupervisor`** (admin) — picks up the EquitySnapshot
   schema fix AND drops the 13 retired bots from `load_bots()`.
3. **Re-fetch NG1 1h bars on canonical / rollover-adjusted source**
   before re-evaluating ng_sweep_reclaim (current audit number is
   data-quality noise per existing demoted_reason).
4. **Hydrate MBT/MET 5m bars** so mbt_sweep_reclaim, met_sweep_reclaim,
   mbt_overnight_gap can be evaluated (currently zero trades = no data).
