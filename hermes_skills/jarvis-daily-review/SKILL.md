---
name: jarvis-daily-review
version: 1.0.0
description: End-of-session trading review — fleet stats, dark modules, top winners/losers, kaizen-held actions, anomalies in today's consult trace.
tags: [trading, review, end-of-day, scheduled]
trigger_phrases:
  - "daily review"
  - "end-of-day review"
  - "session recap"
  - "how did today go"
  - "what happened today"
---

# jarvis-daily-review

End-of-session reflection that turns 24h of raw JARVIS consult traces, kaizen actions, and fleet metrics into a 7-section operator brief. Designed to fire at 3pm ET (1 hour before close) OR on operator demand. Auto-saves durable insights to long-term memory.

## When to invoke

* Operator types "daily review", "session recap", "how did today go", or similar.
* `morning_briefing` cron's evening counterpart triggers this skill at 15:00 ET via the Hermes scheduler.

## Output template

```
═══════════ JARVIS DAILY REVIEW · {ISO date} ═══════════

1. Fleet headline
   {N} bots active · {tier_counts} · session R: {today_R}

2. Top 3 contributors
   {bot_id}  +{R}R  ({n_trades} trades)
   ...

3. Top 3 detractors
   {bot_id}  -{R}R  ({n_trades} trades)
   ...

4. Anomalies (consults where final_verdict != cascade-expected)
   {consult_id}  {bot}  {action}  {block_reason or surprise}

5. Kaizen status
   HELD actions awaiting 2nd confirmation: {n}
     - {bot_id}  ({action})  hold-age={n_hours}h
   APPLIED today: {n}

6. Hermes overrides active
   size_modifiers: {n_pinned}  ({bots})
   school_weights: {n_pinned}  ({assets})

7. Operator action items
   • {one-line each, max 3}

══════════════════════════════════════════════════════════
```

## Required tool sequence

1. `jarvis_fleet_status` — pull tier counts + top5_elite / top5_dark
2. `jarvis_subscribe_events(stream="trace", since_offset=0, limit=500)` — read today's trace
3. `jarvis_subscribe_events(stream="kaizen", since_offset=0, limit=50)` — kaizen actions today
4. `jarvis_active_overrides` — current pins
5. `jarvis_wiring_audit` — any dark modules to flag in §1 footer

## Memory save discipline

After generating the review, save up to 3 durable facts via the `fact_store` tool. Examples:

* "atr_breakout_mnq has been the #1 contributor 4 days running" → operator preference reinforcement
* "Sharpe of vp_mnq dropping faster than 2-week MA" → regime warning
* "MNQ school weight `momentum=1.2` was pinned all day, contributed +2R" → strategy validation

DO NOT save daily P&L numbers, individual trade outcomes, or transient session state — those are recoverable from the trace and would bloat memory.

## Edge cases

* **Empty trace**: report `"No consults today (supervisor inactive or weekend)"` and skip §2–4.
* **HELD action stale**: if a HELD action is >24h old without a 2nd confirmation, flag in §5 as `STALE — needs operator decision or auto-expire`.
* **Anomaly count > 5**: list only top 5 by `block_reason` frequency; mention `+N more` at end.

## Tone

Direct. No hedging. The operator wants signal density, not narrative. Bullet-point everything except section headers. If a section is empty, render `(none)` rather than omitting the section — visual rhythm matters at 3pm when the operator is rushing to close.
