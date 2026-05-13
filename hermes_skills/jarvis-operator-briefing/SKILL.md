---
name: jarvis-operator-briefing
version: 1.0.0
description: PnL-first operator briefing — replaces the noisy "watchdog autohealed" cron content with real trading info (PnL, recent trades, winners/losers). Auto-suppresses on quiet windows.
tags: [trading, briefing, pnl, telegram, summary]
trigger_phrases:
  - "operator briefing"
  - "pnl briefing"
  - "what's my pnl"
  - "trading summary"
  - "morning briefing"
  - "session brief"
  - "how am I doing"
  - "show me my pnl"
---

# jarvis-operator-briefing — Real PnL Telegram Brief

Operator's "what's actually going on with my money" report. Replaces
the generic fleet_status content that produced "watchdog autohealed"
noise with REAL trading info: PnL, trades, winners, losers.

Material-suppress contract
--------------------------

This skill HONORS a suppression contract. When activated by a scheduled
cron task, it first calls `jarvis_material_events_since(asof=<last_cron_fire>)`.
If `has_material=false` (no trades, no big moves, no overrides changed),
it replies with EXACTLY the single word "quiet" — which the Hermes
scheduled-task layer treats as "do not deliver."

The operator gets messages ONLY when something material happened.
No more spam during sleep hours or weekends.

## When to invoke

| Trigger | Behavior |
|---|---|
| Cron `morning_briefing` (06:30 UTC) | Always deliver (operator wants the morning anchor) |
| Cron `zeus_briefing` (13:30 UTC = 9:30 ET) | Always deliver (market-open anchor) |
| Cron `daily_review` (19:00 UTC weekdays) | Always deliver (3pm ET anchor) |
| Cron `weekly_review` (Sun 20:00 UTC) | Always deliver (week wrap) |
| Cron `pre_event_scanner` (every 15m) | Suppress unless event severity ≥ 2 (existing) |
| Cron `pnl_pulse` (every 2h during market hours) | Material-suppress (only deliver if something happened) |
| Operator-on-demand ("pnl briefing") | Always deliver |

## Tool sequence

1. (If scheduled task) `jarvis_material_events_since(asof_iso=<previous_cron_fire>)`
   * If `has_material=false`: reply "quiet" and STOP.
   * Else: continue.
2. `jarvis_pnl_multi_window` → today / 7d / 30d PnL bundle
3. `jarvis_zeus(force_refresh=false)` → fleet_status, regime, overrides, upcoming events
4. `jarvis_cost_today` → today's LLM spend (for the footer)
5. (Optional) `fact_store action=search query="operator playbook"` →
   recall any saved patterns relevant to the current regime

## Output template

```
═══════════ JARVIS · {ISO date} ═══════════

📊 PnL
  Today  : {±X.X}R · {n_trades} trades · W/L {n_w}/{n_l} · win rate {win%}
  7-day  : {±X.X}R · {n_trades} trades · {win%}
  30-day : {±X.X}R · {n_trades} trades · {win%}

🏆 Today's wins / 💧 losses
  {bot_a}  +{R}R    │  {bot_x}  {R}R
  {bot_b}  +{R}R    │  {bot_y}  {R}R
  {bot_c}  +{R}R    │  {bot_z}  {R}R

📈 Last 5 trades (newest first)
  {hh:mm}  {bot}  +{R}R  W
  {hh:mm}  {bot}  -{R}R  L
  ...

🏛️ Fleet
  {n_bots} bots · {tier_counts}
  Regime: {regime_label} (confidence {0.XX})
  Active overrides: {n_size} size · {n_school} school
  Dark modules: {n_dark or "none"}

⏰ Upcoming (next 60 min)
  {event_summary or "(none)"}

⚙️ Infrastructure
  LLM today: ${X.XX} · {n_calls} calls
  Health: 9/9 PASS · status http://127.0.0.1:8643

═══════════════════════════════════════════
```

## Discipline rules

* **PnL section ALWAYS leads.** Operator opens the message; they want
  to see "today's R" before anything else.
* **Wins on the LEFT, losers on the RIGHT** — visual balance is on
  purpose. If there are no losers, the right column says "(none)".
* **Trade list is newest-first.** Latest action is at the top.
* **Recent trades show R with explicit sign** (+0.5R / -1.2R, never
  bare 0.5). Sign-blindness costs the operator brain cycles.
* **Regime is one line.** No paragraph essay — just the label + confidence.
* **Infrastructure is the LAST section, not the first.** Operator
  cares about money first, plumbing last.

## What this replaces

Before this skill:

```
morning_briefing prompt was:
  "Run jarvis_fleet_status and jarvis_wiring_audit. Render a 5-line
   morning briefing: today's expected risk, dark modules, top 3
   elite bots by Sharpe, any held RETIRE candidates."

→ rendered output operator saw:
  "5 dark modules: sage.bayes ... · top elite bot atr_breakout_mnq ...
   no retire candidates."

→ operator's reaction: "this is plumbing trivia, where's my PnL?"
```

After this skill:

```
morning_briefing prompt is:
  "Activate jarvis-operator-briefing. Render the PnL-first format."

→ rendered output operator sees:
  "📊 PnL · Today: +1.5R · 7d: +6.8R · 30d: +12R
   🏆 atr_breakout_mnq +0.7R · vp_mnq +0.5R · btc_mom +0.3R
   📈 Last 5: 09:43 atr_breakout +0.4R W, 09:15 vp_mnq +0.3R W..."

→ operator's reaction: "actually useful."
```

## Material-suppress rationale

The operator's Telegram should be **dense, not chatty**. If a 2-hour
window had zero trades, zero R movement, and no override changes —
sending a "still alive" message just trains the operator to ignore
Telegram. By suppressing those windows, the messages the operator
DOES see are guaranteed to be material.

The `jarvis_material_events_since` tool checks 4 triggers:

1. **Trades since asof**: any new closed trade → material
2. **|R| delta ≥ 0.5**: cumulative PnL moved meaningfully → material
3. **Big win/loss**: any single trade ≥ ±2R → material
4. **New override applied**: operator-pinned size/school change → material

A scheduled task that gets "quiet" replied from this skill should
NOT deliver to Telegram. The Hermes scheduled-task layer handles
this via the `deliver_only_if_changed` or similar pattern in its
delivery_extra block.

## On-demand variant

When the operator types "operator briefing" / "pnl briefing" / "how am
I doing" — ALWAYS deliver the full briefing, even on quiet windows.
The material-suppress is for SCHEDULED tasks, not operator queries.

## Memory anchor

Once a week (Sunday weekly_review window) save:

> subject="weekly pnl"
> predicate="week of {YYYY-MM-DD}"
> object="7d: {±X.X}R · win rate {Y}% · top: {bot_a} +{Z}R · worst: {bot_b} -{Z}R"
> trust_score=0.8

This builds the operator's longitudinal performance memory. After 8
weeks of saves, jarvis-trade-narrator's weekly synthesis recalls
patterns automatically.

## Edge cases

* **No trades in the window** — show "(no trades today)" in the PnL
  section, but STILL deliver the regime + overrides + upcoming-events
  sections (those have value even on quiet days).
* **All bots paused** — flag prominently: "⏸️ {n} bots paused via
  size_modifier=0 overrides — clear via 'resume from weekend' or wait
  for TTL."
* **Hermes/JARVIS health degraded** — flag prominently with a 🔧 icon
  before the operator scrolls to the infrastructure footer.

## Why this is one skill, not five

Could have made `jarvis-morning`, `jarvis-3pm`, `jarvis-pnl-pulse`,
`jarvis-weekly` separately. ONE skill with the material-suppress
gate is simpler:
* Same template across all triggers (operator's eyes train to it).
* Single place to evolve the format.
* Material-suppress lives in one place, not five.

The cron tasks just pass different `since_hours` windows + different
trigger contexts.
