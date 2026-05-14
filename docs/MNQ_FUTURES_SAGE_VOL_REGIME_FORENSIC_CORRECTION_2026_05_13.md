# mnq_futures_sage Vol-Regime Forensic — CORRECTION (2026-05-13)

**Status:** This doc supersedes the vol_low_size_mult=0.0 recommendation
in `MNQ_FUTURES_SAGE_VOL_REGIME_FORENSIC.md` and the "single config flip
away" line in `WAVE25_MASTER_SYNTHESIS_2026_05_13.md`.

**Bottom line:** The 100% / 16.7% WR split between qty<1 and qty>=1 on
mnq_futures_sage is a **structural artifact of the supervisor's
partial-profit mechanism**, not evidence of a vol-regime edge. Setting
`vol_low_size_mult=0.0` would be a no-op AND would not address the
USD-negative outcome anyway. The bot is not "one config flip" away from
launch-ready.

---

## What wave-25o got right

The R-vs-USD divergence is real. mnq_futures_sage has:
- 109 trades, +136.9R cumulative, **−$255 cumulative USD**
- qty<1 cohort: 24/24 wins, +$485, tight outcomes
- qty=1.0 cohort: 4/24 wins, −$740, wide outcomes

These numbers are correct. The bot IS R-positive but USD-negative. That
finding stands.

## What wave-25o got wrong

The interpretation: "high-vol regime halves size to 0.5, normal-vol
regime stays at 1.0, the strategy refuses high-vol setups it doesn't
have conviction on, vol_low_size_mult=0.0 skips the bad-cohort trades."

That mechanism doesn't exist on mnq_futures_sage:

### Evidence #1: mnq_futures_sage doesn't use vol_adjusted_sizing

`mnq_futures_sage` uses `strategy_kind="orb_sage_gated"`
(`per_bot_registry.py:612`), which maps to `SageGatedOrbStrategy` in
`strategies/sage_gated_orb_strategy.py`. The vol-adjusted sizing fields
(`vol_high_size_mult`, `vol_low_size_mult`, `vol_high_threshold`,
`vol_low_threshold`) live in `SweepReclaimConfig` —
**they don't exist on `SageGatedOrbConfig`**. Setting
`vol_low_size_mult=0.0` in the bot's per-bot registry extras would be
silently ignored.

### Evidence #2: The qty=0.5 cohort is the supervisor's partial-profit slice

`jarvis_strategy_supervisor.py:4319` (`_maybe_take_partial_profit`):

```python
trigger_r = self._env_float("ETA_PARTIAL_PROFIT_R", 1.0)   # +1R
pct = self._env_float("ETA_PARTIAL_PROFIT_PCT", 0.5)        # 50%
...
partial_qty = full_qty * pct                                # = qty * 0.5
pos["qty"] = partial_qty
rec = self._router.submit_exit(...)                         # emits close with qty=0.5
runner = {...}
runner["qty"] = remaining_qty                               # remaining 50%
runner["partial_taken"] = True
```

So when a position reaches +1.0R:
1. **Close record A** emitted: `qty=0.5`, realized_r ≈ +1.0 (the partial)
2. Runner continues with qty=0.5, partial_taken=True, stop trailed
3. Eventually emits **Close record B**: `qty=0.5`, realized_r = whatever the runner hits

For a position that never reaches +1.0R:
1. **Single close record**: `qty=1.0`, realized_r = stop hit (typically −1.0)

This explains the cohort split exactly:
- **qty=0.5 cohort (24/24 wins, +$485)** = trades that DID reach +1.0R.
  Half of those records are guaranteed wins (the partial slice firing
  at +1R), half are runner outcomes (mostly trail-locked profits since
  partial confirmed the move was working).
- **qty=1.0 cohort (4/24 wins, −$740)** = trades that NEVER reached
  +1.0R. They got stopped at original stop for the full −1R loss.

The 100% WR on qty=0.5 isn't an edge — **it's a tautology**. Partial
slices, by construction, only fire when the trade is profitable.

### Evidence #3: vol_low_size_mult semantics don't match the forensic's claim

Even if mnq_futures_sage used SweepReclaim (it doesn't), the
`vol_low_size_mult` field affects the **LOW-vol** case
(`ratio <= vol_low_threshold`), not normal-vol:

```python
# sweep_reclaim_strategy.py:393
if ratio >= self.cfg.vol_high_threshold:
    risk_usd *= self.cfg.vol_high_size_mult       # high vol -> 0.5
elif ratio <= self.cfg.vol_low_threshold:
    risk_usd *= self.cfg.vol_low_size_mult        # low vol  -> field in question
# else: ratio in normal band — baseline size (no multiplier)
```

To "skip normal-vol setups entirely" on SweepReclaim, you'd need to add
a `vol_normal_size_mult: float = 0.0` field AND apply it in the else
branch. Not a config flip — a code change.

## What the bot's real problem is

mnq_futures_sage is **R-positive but USD-negative because losses and
wins exit at structurally different position sizes**:

- **Losers** exit at original stop with **qty=1.0** (full risk in USD)
- **Winners** exit half at partial (qty=0.5) and half at target/trail
  (qty=0.5) — so the winning trade's total USD outcome is split across
  two records each at 50% size

When you sum the records: the loser column = 24 × $30 loss = $720, but
the winner column = 24 × $20 gain = $480. Net USD = −$240. Matches the
−$255 observed (small rounding from the 61 untagged records).

**This is the same bug `MES_V2_SIZING_FORENSIC.md` Fix A and Fix C
were designed to address** (constant-USD risk sizing), not a vol-regime
filter problem.

## What would actually move mnq_futures_sage toward launch-ready

Three honest options, none "one config flip":

### Option 1: Disable supervisor partial-profit on this bot

Add `ETA_PARTIAL_PROFIT_ENABLED=false` only for mnq_futures_sage's
position-management path. All trades then go to original target/stop
at full size, R↔USD alignment restored. Requires per-bot scoping of
the partial-profit ENV flag (currently global).

**Cost:** Loses the partial-profit safety net entirely. Winners might
give back gains if they reverse before target.

### Option 2: Adopt constant-USD risk sizing (Fix C from MES_V2 forensic)

Replace `qty = risk_usd / stop_dist` with a fixed dollar-risk model.
The trade-off: position size becomes a function of stop distance —
wide stops produce small positions, tight stops produce large
positions, all sized to the same dollar risk. This DOES address the
qty=1.0 vs qty=0.5 USD asymmetry because the supervisor's partial
slice would still be 50% of the entry qty, but the entry qty would
be sized to (say) $100 risk per trade rather than 1% of equity.

**Cost:** Larger code surface, needs paper-soak from scratch on new
position size profile.

### Option 3: Tighten time-stops so qty=1.0 losers don't bleed full -1R

Force position close after N bars of no progress so the
"never-reaches-partial" cohort gets out faster. Trades that aren't
working close at -0.3R instead of -1.0R, narrowing the USD gap.

**Cost:** Loses occasional slow-developing winners; needs tuning of
the time-stop window per bot.

### Option 4: Accept the verdict and look elsewhere

mnq_futures_sage's broader history (1267 trades, +0.82R avg, 55% WR
per per_bot_registry.py:614) was profitable BEFORE partial-profit was
enabled supervisor-wide. The right move may be to retire this bot
from the EVAL_LIVE pin and find a strategy that doesn't have the
partial-profit/full-stop asymmetry baked in.

## Pre-committed falsification (unchanged)

> If no bot crosses all five launch-candidate criteria by 2026-06-01,
> the strategy family is structurally unprofitable in USD terms, and
> the operator must redesign the qty sizing logic before any further
> launch attempt.

**Setting `vol_low_size_mult=0.0` does not satisfy this falsification.**
Don't make that change and call it a paper-soak experiment.

## What the wave-25 system still did right

The discipline of trusting `NO_GO` is correct. The wave-25o forensic
found the R-vs-USD divergence pattern even though it misidentified the
mechanism. That's how systematic debugging works: surface the anomaly,
get the diagnosis wrong on the first pass, correct on the second pass,
ship the fix on the third pass. We're on pass 2.

---

## Cross-reference

- `docs/MNQ_FUTURES_SAGE_VOL_REGIME_FORENSIC.md` — original (wrong) forensic
- `docs/WAVE25_MASTER_SYNTHESIS_2026_05_13.md` — points at the wrong recommendation
- `docs/MES_V2_SIZING_FORENSIC.md` — Fix A / Fix C constant-USD risk proposals
- `docs/FLEET_QTY_BUG_AUDIT.md` — fleet-wide pattern (likely also misdiagnosed)
- `strategies/sage_gated_orb_strategy.py` — `SageGatedOrbConfig`, no vol fields
- `strategies/sweep_reclaim_strategy.py` — `SweepReclaimConfig`, vol fields exist
- `scripts/jarvis_strategy_supervisor.py:4319` — `_maybe_take_partial_profit`
