# Launch Candidate Scan — 2026-05-13 (5 days before planned cutover)

**Question asked:** Which bot, if any, is genuinely safe to promote to
`EVAL_LIVE` on Monday 2026-05-18?

**Answer:** None. The data is honest about not being ready.

> **Historical snapshot note:** This memo captures a dated 2026-05-13 scan.
> Use `python -m eta_engine.scripts.prop_launch_check --json` as the current
> Diamond/Wave-25 launch authority before acting on any older scan result.

---

## Method

Scan every bot with ≥5 production-filtered (`live + paper`) trade-close
records on VPS. Apply the **launch-candidate criteria**:

1. `n_trades >= 50` (sample sufficient for any inference)
2. `cum_USD > 0` (real-money outcome is profitable)
3. `cum_R > 0` (R-edge backs up the USD)
4. `win_rate >= 50%`
5. NOT flagged by the qty-asymmetry audit

A bot meeting **all five** is a defensible launch candidate.

---

## Scan results (VPS, production_strict filter)

| bot_id | n | WR | cum_R | cum_USD | wr@qty=1 | wr@qty<1 | flag |
|---|---|---|---|---|---|---|---|
| `mnq_futures_sage` | **109** | 64.2% | +136.9R | **−$255** | 16.7% | 100.0% | **ASYM** |
| `nq_futures_sage` | 60 | 56.7% | −2.6R | −$782 | 33.3% | — | (no qty<1 sample) |
| `met_sweep_reclaim` | 20 | 70.0% | +0.0R | n/a | — | — | (too small) |
| `ng_sweep_reclaim` | 10 | 60.0% | +0.7R | −$253 | 60.0% | — | (too small) |
| `mcl_sweep_reclaim` | 8 | 25.0% | −4.5R | −$187 | 0.0% | 100.0% | (too small) |

**Zero bots qualify.** The two bots with sample size ≥50:
- `mnq_futures_sage` is PROP_READY but `cum_USD = −$255` AND flagged
  ASYM by the qty audit (wr@qty<1 = 100%, wr@qty=1 = 16.7%)
- `nq_futures_sage` has `cum_R = −2.6` and `cum_USD = −$782` — losing on both axes

---

## What this means

1. **The wave-25 system is functioning correctly.** Its job was to
   refuse to launch on inadequate or contradictory evidence. It is
   refusing. That's the correct behavior.

2. **The R-vs-USD divergence is fleet-wide, not isolated to mes_v2.**
   The same fractional-qty winner-loser asymmetry that flipped mes_v2
   USD-negative is showing up on `mnq_futures_sage` and `mcl_sweep_reclaim`
   too. This is a strategy-family bug in the sweep_reclaim sizing
   formula AND in the futures_sage sizing path.

3. **Promoting any of these bots to `EVAL_LIVE` Monday would burn the
   eval.** The eval costs $59 to replace, but burning it teaches the
   operator nothing they don't already know from this audit.

4. **The wait-for-green strategy works** because the supervisor is
   in `paper_live` mode (live data, paper fills) on VPS, accumulating
   real-fidelity paper data 24/7. Every trade builds the sample. The
   QtyAsymmetryDaily + LivePaperDriftDaily + Wave25StatusHourly crons
   surface drift the moment a real launch candidate emerges.

---

## Recommendation to the operator

**Do not launch live Monday 2026-05-18.**

Instead:

1. **Sunday EOD**: run `prop_launch_check`. Verdict will be `NO_GO`. Trust it.
2. **Monday morning**: let the supervisor continue running in `paper_live` mode.
   No bots in `EVAL_LIVE`. No live capital at risk. Wave-25 keeps building
   evidence.
3. **Daily check-in**: re-run `prop_launch_check` each EOD. When at least
   one bot crosses ALL FIVE candidate criteria, the system will say so.
4. **Pre-commit a falsification criterion**: "if no bot crosses the
   threshold by 2026-06-01, the strategy family is structurally
   unprofitable in USD terms, and the operator must redesign the qty
   sizing logic before any further launch attempt."

Scope note: `prop_launch_check` is the Diamond/Wave-25 launch-candidate
cutover verdict. The separate futures prop-ladder controlled dry-run lane for
`volume_profile_mnq` can remain blocked independently. Check that parallel
lane with:

```powershell
python -m eta_engine.scripts.prop_live_readiness_gate --json
python -m eta_engine.scripts.prop_operator_checklist --json
python -m eta_engine.scripts.prop_strategy_promotion_audit --json
```

The $59 eval account stays untouched. The wave-25 infrastructure
keeps surfacing the truth.

---

## What investigation should happen during the wait

While paper data accumulates:

1. **Fix the qty asymmetry** at the strategy level. Implement Fix C
   from `MES_V2_SIZING_FORENSIC.md` (constant-USD risk instead of
   constant-R risk) so paper-fill records start showing USD-positive
   alongside R-positive.

2. **Reconcile mnq_futures_sage's qty<1 sample.** Currently 100% WR
   on qty<1 trades — that's suspiciously perfect. Either the sample
   is too small to be meaningful (verify n), or there's something
   structural about the qty<1 setup conditions that genuinely
   produces 100% winners (verify by checking distinct signal IDs).

3. **Re-run this scan weekly.** When `n_launch_candidates >= 1` and
   `prop_launch_check` turns `GO`, the operator has a real launch review
   signal. Until then, paper-only.

---

## Reproduction

```python
python -m eta_engine.scripts.diamond_qty_asymmetry_audit
python -m eta_engine.scripts.diamond_leaderboard
python -m eta_engine.scripts.prop_launch_check
python -m eta_engine.scripts.prop_live_readiness_gate --json
python -m eta_engine.scripts.prop_operator_checklist --json
python -m eta_engine.scripts.prop_strategy_promotion_audit --json
```

The `_find_launch_candidate.py` one-shot script that generated this
scan is intentionally not committed — its logic is captured in this
memo and is trivially reconstructable from the existing audit
infrastructure.

---

## Cross-reference

- `docs/MES_V2_SIZING_FORENSIC.md` — original fractional-qty finding
- `docs/FLEET_QTY_BUG_AUDIT.md` — fleet-wide extension
- `docs/WAVE25_PROP_LAUNCH_OPS.md` — wave-25 gate architecture
- `docs/MONDAY_MORNING_OPERATOR_RUNBOOK.md` — operator launch sequence
- `docs/PROP_FUND_ROLLBACK_RUNBOOK.md` — rollback if launched and bled
