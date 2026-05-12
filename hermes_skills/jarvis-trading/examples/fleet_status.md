# Example: fleet status

**Operator:** what's the fleet doing?

**Hermes:** *(invokes `jarvis_fleet_status`)*

> 14 bots active across 3 asset classes.
> Tiers: 4 elite, 7 standard, 3 dark.
> Actions last kaizen: 0 DEPLOY, 1 RETIRE (held — 1st of 2 runs).
> Top elites by Sharpe: `mnq_breakout_v3` (2.31), `gold_trend_v2` (1.94), `crude_rev_v1` (1.78).
> Wiring audit: all 9 modules reporting.

**Operator:** what's the retire candidate?

**Hermes:** *(invokes `jarvis_trace_tail`)* `eth_perp` — flagged for Sharpe drift below 0.5 over the last 14 sessions. Recommendation appeared once; needs a confirming run before it can apply. The 2-run gate is the kaizen safety latch — JARVIS will hold until tomorrow's 06:00 loop confirms.
