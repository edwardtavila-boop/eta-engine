---
name: jarvis-pre-event-prep
version: 1.0.0
description: 30-minutes-before-event prep brief — econ event impact analysis, current fleet exposure, suggested de-risk actions before known volatility.
tags: [trading, event, prep, calendar]
trigger_phrases:
  - "event prep"
  - "FOMC prep"
  - "CPI prep"
  - "what's coming up"
  - "next event"
auto_trigger:
  event_stream: dashboard
  condition: "upcoming_event.severity >= 2 AND minutes_to_event <= 30"
  rate_limit_minutes: 30
---

# jarvis-pre-event-prep

Fires 30 minutes before a high-severity econ event (FOMC, CPI, NFP, GDP, OPEX). Combines the event calendar with current fleet exposure to produce a pre-event de-risk brief.

## When to invoke

* **Scheduled (live today)**: the `pre_event_scanner` cron job (in `~/.hermes/config.yaml scheduled_tasks`) runs every 15 minutes and calls `jarvis_upcoming_events(horizon_min=30)`. When a severity ≥ 2 event is within 30 minutes, the scheduled task activates THIS skill and delivers the brief. When nothing's pending the task returns "quiet" and Hermes suppresses delivery.
* **Manual**: "FOMC prep", "CPI is in 30 minutes", "what's coming up", etc.
* **Auto-trigger (aspirational future)**: the `auto_trigger:` frontmatter declares the intended condition for a future event-driven conductor that would fire on the event stream rather than on a fixed 15-min poll. Lower priority because the scheduled task already gives near-real-time coverage.

## Tool chain

1. `jarvis_upcoming_events(horizon_min=60)` — confirm the event(s) and severity.
2. `jarvis_fleet_status` — current active bots, tier distribution.
3. `jarvis_portfolio_assess(bot_id="<each active bot>", asset_class=<event_currency>, action="ENTER")` — does the portfolio brain currently allow new entries for the event currency? (If `size_modifier < 0.5` it's already self-de-risking.)
4. `jarvis_active_overrides` — pre-existing operator pins to respect.
5. `fact_store action=search query="event prep {event_kind}"` — recall what happened in prior FOMC/CPI events.

## Output template

```
⏰ PRE-EVENT BRIEF · T-{minutes}m to {event_kind} {symbol}

Event:
  {kind}  severity={severity}/3  at {ts_utc}
  consensus: {if available}   prior: {if available}

Current fleet exposure (event-related):
  long  {asset}: ${notional_long}k  ({n_long} bots)
  short {asset}: ${notional_short}k ({n_short} bots)

Portfolio brain stance:
  {bot_id}  →  size_modifier={value}  ({notes})
  ... (worst 3)

Historical playbook (memory):
  {recalled facts or "no prior {event_kind} record"}

Suggested actions (NONE are auto-applied):
  A) Trim all event-asset bots to 0.3× via `jarvis_set_size_modifier` (TTL 60m)
  B) Pin school weights toward defensive schools (e.g. mean_revert ↑1.2, momentum ↓0.8)
  C) Stay the course — portfolio brain already self-de-risked enough
  D) Do nothing — event is low-severity for our fleet

Operator: A/B/C/D ?
```

## Action policy

* **Suggest A** when ≥30% of fleet notional is in the event currency AND portfolio brain hasn't already self-trimmed (any bot's size_modifier > 0.8 for the event asset).
* **Suggest B** when the historical playbook shows that mean_revert/defensive schools outperformed during this event_kind in past 3 occurrences.
* **Suggest C** when portfolio brain has already done its job (everything is below 0.5×).
* **Suggest D** when exposure to event currency is <10% of fleet notional or severity ≤1.

## Action execution

If operator picks A: call `jarvis_set_size_modifier` for each top-3 event-asset bot with `modifier=0.3, ttl_minutes=60`.
If B: call `jarvis_pin_school_weight` for the (asset, school) pairs identified.
If C or D: no tool calls — just acknowledge.

## Memory save

After the event passes (operator says "event done" or 60 minutes elapse), append:

> subject="event playbook:{event_kind}:{event_symbol}",
> predicate="operator chose",
> object="option {A|B|C|D}: outcome was {fleet_pnl_during_window} R",
> trust_score=0.8

Build the playbook over time. After 5 FOMC events the skill recalls "operator usually picks B, mean_revert overlay has averaged +0.8R during FOMC".

## Edge cases

* **Already-pinned size_modifiers**: respect them. Suggest A only on bots not already pinned.
* **Kill switch active**: skip the brief — say "fleet kill is active, no event response needed".
* **Multiple events within 30m** (e.g. CPI + retail sales same morning): list all, but recommend a single action that covers all events (usually most-severe-wins).
