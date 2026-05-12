# Example: 06:30 morning briefing

This is the scheduled-task output the operator receives via Telegram daily at 06:30, 30 minutes after the 06:00 ETA-Kaizen-Loop completes.

**Scheduled prompt (from `manifest.yaml`):**

> Run `jarvis_fleet_status` and `jarvis_wiring_audit`. Render a 5-line morning briefing: today's expected risk, dark modules, top 3 elite bots by Sharpe, any held RETIRE candidates.

**Hermes renders (delivered to Telegram):**

> ETA morning briefing — 2026-05-11 06:30 ET
>
> Expected risk: 4.2R (within daily budget of 6R).
> Dark modules: none — full wiring.
> Top 3 elite Sharpe: `mnq_breakout_v3` (2.31), `gold_trend_v2` (1.94), `crude_rev_v1` (1.78).
> Held RETIRE candidates: 1 — `eth_perp` (Sharpe drift; 1 of 2 confirming runs).
> No DEPLOY recommendations today.

**If a dark module is detected:**

> ETA morning briefing — 2026-05-11 06:30 ET
>
> Expected risk: 4.2R (within daily budget).
> Dark modules: `event_calendar` not reporting since 04:12 ET — JARVIS is making decisions without econ-event veto. Investigate before US session open.
> Top 3 elite Sharpe: `mnq_breakout_v3` (2.31), `gold_trend_v2` (1.94), `crude_rev_v1` (1.78).
> Held RETIRE candidates: 0.
> Held DEPLOY candidates: 0.
