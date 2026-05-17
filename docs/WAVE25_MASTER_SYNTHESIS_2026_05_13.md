# Wave-25 Master Synthesis — 2026-05-13

**Hard deadline:** 2026-05-18 (Monday) prop-fund cutover on $50K BluSky eval.
**Verdict:** **NO_GO** for Monday. The system was built to refuse this launch
and it is refusing. Trust it.

---

## What we built (wave-25 a→o)

A 24/7 conditional-routing supervisor that runs every strategy in
`paper_live` mode (live data, paper fills) on the VPS, surfaces per-bot
drift and asymmetry daily, and **gates promotion to live execution
behind five hard launch-candidate criteria**. No bot has crossed those
criteria. Therefore no bot ships live Monday.

Stack landed (commits on `origin/main`):

| Wave | Commit | What it added |
|---|---|---|
| 25a–c | (multiple) | lifecycle states + alert dispatcher + status surface |
| 25d | (multiple) | `manage_lifecycle.py`, `verify_telegram.py` |
| 25e | (multiple) | gate consolidation — unified pre-trade entry gate |
| 25f–h | (multiple) | dual-view ledger, shadow signal logger |
| 25i | (multiple) | `prop_launch_check` single-command verifier |
| 25j | (multiple) | `MONDAY_MORNING_OPERATOR_RUNBOOK.md` |
| 25l | 7dbff3a | `mes_v2` forensic + drift detector + first-light check |
| 25m | 0a974f4 | fleet qty-asymmetry audit + cron registration |
| 25n | 1064f3b | launch-candidate gate + honest verdict |
| **25o** | **b8ab641** | **mnq_futures_sage vol-regime forensic + per-qty-band breakdown** |

---

## What the audits found

### Finding #1: R-vs-USD divergence is fleet-wide (wave-25l, 25m)

`mes_v2` is R-positive on backtest but USD-negative on paper-live. Root
cause: fractional qty asymmetry — winners cluster at qty<1 (half-size,
small dollars) while losers cluster at qty=1 (full-size, big dollars).
The same pattern showed up on `mnq_futures_sage` and `mcl_sweep_reclaim`.

### Finding #2: Zero launch candidates on VPS production data (wave-25n)

The launch-candidate gate requires all five of:
1. `n_trades >= 50`
2. `cum_USD > 0`
3. `cum_R > 0`
4. `win_rate >= 50%`
5. NOT flagged ASYMMETRY_BUG

| Bot | n | WR | cum_R | cum_USD | Verdict |
|---|---|---|---|---|---|
| `mnq_futures_sage` | 109 | 64.2% | +136.9R | **−$255** | REJECT (USD-neg + ASYM) |
| `nq_futures_sage` | 60 | 56.7% | −2.6R | −$782 | REJECT (R-neg + USD-neg) |
| `met_sweep_reclaim` | 20 | 70.0% | +0.0R | n/a | too small |
| `ng_sweep_reclaim` | 10 | 60.0% | +0.7R | −$253 | too small |
| `mcl_sweep_reclaim` | 8 | 25.0% | −4.5R | −$187 | too small |

**Zero pass.** The system says do not launch. The system is right.

### Finding #3: mnq_futures_sage's asymmetry is `vol_adjusted_sizing` working as designed (wave-25o)

Splitting `mnq_futures_sage`'s 109 records by qty band:

| Cohort | n | WR | avg R | sum USD | stops |
|---|---|---|---|---|---|
| qty=1.0 (normal-vol) | 24 | 16.7% | −0.615 | −$740.50 | wide (~174 ticks) |
| qty=0.5 (high-vol, halved) | 24 | **100.0%** | +6.314 | **+$485.50** | tight (~2 ticks) |

The strategy refuses to enter at full size when ATR exceeds the high-vol
threshold; it halves to qty=0.5 instead. The high-vol regime produces
big-R clean winners with tight stops; the normal-vol regime produces
small-R bracket churn. The aggregate `cum_USD = −$255` is the sum of
these two regimes. **Half the trade book is just bad. The other half is
launch-candidate-quality.**

This interpretation was later corrected. The qty<1 vs qty>=1 split on
`mnq_futures_sage` was traced to the supervisor partial-profit mechanism,
not a real vol-regime filter that can be fixed with `vol_low_size_mult=0.0`.
See `MNQ_FUTURES_SAGE_VOL_REGIME_FORENSIC_CORRECTION_2026_05_13.md`.

---

## What ships in wave-25o (today)

1. `docs/MNQ_FUTURES_SAGE_VOL_REGIME_FORENSIC.md` — full forensic with
   sample-size caveats and recommended path forward.
2. `scripts/prop_launch_check.py` — now computes per-qty-band stats
   (qty<1 vs qty≥1) on every bot and surfaces `vol_regime_filter_candidate`
   when a bot's qty<1 sub-cohort meets the launch profile.
3. `tests/test_prop_launch_check_qty_bands.py` — 6 tests covering
   strict candidates, filter candidates, edge cases, and mutual
   exclusion. All passing.

Sunday-EOD operator now sees, in one CLI run:

```
mnq_futures_sage  n= 109  WR=64.2%  cum_R=+136.9  cum_USD=$-255 ASYM *VOL_FILTER
    qty<1: n= 24 WR=100.0% USD=  +$486    qty>=1: n= 30 WR= 16.7% USD=  -$740
```

Action item #2 in the CLI originally auto-surfaced:

> "VOL-REGIME FILTER candidate: mnq_futures_sage — set
> `vol_low_size_mult=0.0` in the bot's strategy config to skip
> normal-vol setups; paper-soak for 2 weeks before EVAL_LIVE."

---

## Recommended path forward (operator)

### Sunday 2026-05-17 EOD

Run:

```powershell
python -m eta_engine.scripts.prop_launch_check
```

The verdict will be `NO_GO`. Trust it.

Scope note: `prop_launch_check` is the Diamond/Wave-25 launch-candidate
cutover verdict. If the separate futures prop-ladder controlled dry-run lane
for `volume_profile_mnq` matters too, read it in parallel via
`prop_live_readiness_gate`, `prop_operator_checklist`, and
`prop_strategy_promotion_audit`.

### Monday 2026-05-18 morning

**Do not launch live.** Let the supervisor keep running in `paper_live`
mode. The eval ($59 sunk cost) stays untouched. Wave-25 keeps
accumulating real-fidelity paper data.

### Optional Sunday-night experiment

If the operator wants to test the corrected `mnq_futures_sage` hypothesis
BEFORE 2026-06-01, do not use `vol_low_size_mult=0.0`. Instead, run the
bot-scoped paper-soak from
`MNQ_FUTURES_SAGE_VOL_REGIME_FORENSIC_CORRECTION_2026_05_13.md`:

```python
# corrected paper-soak target for mnq_futures_sage
partial_profit_enabled = false
```

Then paper-soak for 2 weeks. The Sunday-EOD `prop_launch_check` will show
show whether the filtered book stays USD-positive at n≥50.

Correction: the older "filtered book" wording above is superseded. The current
live experiment is the corrected `partial_profit_enabled=false` paper-soak,
not a `vol_low_size_mult=0.0` config flip.

### Pre-committed falsification

> If no bot crosses all five launch-candidate criteria by 2026-06-01,
> the strategy family is structurally unprofitable in USD terms, and
> the operator must redesign the qty sizing logic (Fix A: constant-USD
> risk) before any further launch attempt.

That date is the kill-switch. Without it there is no strategy, only
belief.

---

## Sample-size honesty

The qty<1 cohort on `mnq_futures_sage` is 24 trades at 100% WR. The
binomial 95% CI for "P(win) given 24/24 wins" is roughly [0.86, 1.00] —
the true hit rate is at least 86% with high confidence. That's a real
edge IF the high-vol regime stays stable. But:

- 24 trades is small. The strategy could have gotten lucky on a
  particular tape (post-CPI Asia session bounce, etc.).
- The forensic was done on ~5 days of data. Regime stability over weeks
  is what matters for a 30-day eval.
- `mnq_futures_sage`'s broader history (1267 trades, +0.82R avg, 55% WR)
  blends both vol regimes. Whether the high-vol-only book holds up over
  wider historical windows is unknown.
- The corrected `partial_profit_enabled=false` experiment still needs a
  broker-backed post-fix sample. A tiny post-fix close count is not enough
  to reclassify the bot as launch-ready.

**These caveats are why the recommendation is paper-soak, not Monday
launch.**

---

## What wave-25 did right

Caught the bug before it cost the operator the eval. The R-vs-USD
divergence was visible in the data, the qty-asymmetry audit found the
pattern, the launch-candidate gate refused to designate any bot as safe,
and the forensic isolated the actual mechanism. The discipline of
trusting the system's `NO_GO` bought the time for this analysis.

The system protected the operator from a decision their pattern-matching
brain would have made on incomplete evidence. That is the entire reason
it exists.

---

## Cross-reference

- `docs/MES_V2_SIZING_FORENSIC.md` — first R-vs-USD forensic (wave-25l)
- `docs/FLEET_QTY_BUG_AUDIT.md` — fleet-wide qty asymmetry extension (wave-25m)
- `docs/LAUNCH_CANDIDATE_SCAN_2026_05_13.md` — today's NO_GO verdict (wave-25n)
- `docs/MNQ_FUTURES_SAGE_VOL_REGIME_FORENSIC.md` — vol-regime forensic (wave-25o)
- `docs/WAVE25_PROP_LAUNCH_OPS.md` — wave-25 architecture overview
- `docs/MONDAY_MORNING_OPERATOR_RUNBOOK.md` — operator launch sequence
- `docs/PROP_FUND_ROLLBACK_RUNBOOK.md` — rollback if launched and bled

---

## Reproduction (anytime)

```powershell
python -m eta_engine.scripts.prop_launch_check
python -m eta_engine.scripts.prop_live_readiness_gate --json
python -m eta_engine.scripts.prop_operator_checklist --json
python -m eta_engine.scripts.prop_strategy_promotion_audit --json
python -m eta_engine.scripts.diamond_qty_asymmetry_audit
python -m eta_engine.scripts.diamond_leaderboard
python -m eta_engine.scripts.diamond_wave25_status
```

The four cron tasks (15-min ledger refresh, hourly leaderboard +
wave-25-status, daily qty-asymmetry + live-paper-drift) keep the
receipts fresh on the VPS. Sunday EOD just reads them.

---

**End of wave-25 master synthesis. System is doing what it was built
to do. The operator does not launch live Monday. The eval stays
untouched. Wave-25 keeps building evidence for the next decision
window.**
