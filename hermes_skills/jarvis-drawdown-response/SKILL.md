---
name: jarvis-drawdown-response
version: 1.0.0
description: Auto-investigate and propose corrective actions when fleet drawdown exceeds 3R intraday — diagnostic chain + size-trim suggestions + escalation gate.
tags: [trading, risk, drawdown, automatic]
trigger_phrases:
  - "drawdown response"
  - "fleet down"
  - "we're bleeding"
  - "circuit"
  - ">3R"
auto_trigger:
  event_stream: trace
  condition: "portfolio_drawdown_today_r <= -3.0"
  rate_limit_minutes: 30
---

# jarvis-drawdown-response

Triggered automatically (or by operator on demand) when fleet drawdown exceeds 3R intraday. Goal: identify the bleeders, isolate root cause, propose a sized trim, and gate any kill-switch decision behind operator approval.

## When to invoke

* **Manual today**: operator types "drawdown response", "we're bleeding", "fleet is down 3R", etc. Hermes recognizes the trigger phrases above and activates this skill.
* **Auto-trigger (aspirational — not yet wired)**: the `auto_trigger:` block in this frontmatter declares the intended condition for a future conductor that polls `jarvis_subscribe_events(stream=trace)` and fires this skill when `portfolio_drawdown_today_r <= -3.0`. Conductor is planned under the inter-agent-bus track (T14 in the future-tracks menu). Until then, the operator (or a separate scheduled task they wire up) must invoke this skill manually.

## Tool chain (MUST execute in this order)

1. `jarvis_fleet_status` — confirm the drawdown number from the latest kaizen report (auto-trigger could fire on a stale or noisy record).
2. `jarvis_trace_tail(n=50)` — read recent consults, identify which bots emitted losses.
3. For each top-3 bot by negative contribution, call `jarvis_explain_verdict(consult_id=<latest_losing_consult>)`.
4. `jarvis_upcoming_events(horizon_min=120)` — is a known event window driving the drawdown (CPI, FOMC, OPEX)?
5. `fact_store action=search query="drawdown response history"` — recall prior operator decisions in similar episodes.

## Output template

```
⚠  JARVIS DRAWDOWN RESPONSE · session R: {today_R}

Cause cluster (top 3 bleeders):
  1. {bot_id}  -{R}R  ({reason from explain_verdict})
  2. {bot_id}  -{R}R  ({reason})
  3. {bot_id}  -{R}R  ({reason})

External context:
  • Upcoming events ≤2h: {list or "none"}
  • Active overrides: {summary}
  • Prior operator playbook (memory): {recalled facts or "none"}

Recommended actions (operator must confirm each):
  A) Trim top bleeder {bot_id} to 0.3× via `jarvis_set_size_modifier`
  B) Pin school weight for {asset} (drop {school} to 0.5) via `jarvis_pin_school_weight`
  C) Kaizen-retire {bot_id} (2-run gate already partial: {prior})
  D) Kill switch (REQUIRES OPERATOR TYPING "kill all" verbatim)

What would you like to do?
```

## Escalation discipline

* **Always present options A→D in ascending severity.** Operator picks; you execute. Never auto-kill.
* **A and B are TTL-bounded** — defaults to 240 min so the override auto-expires when the session ends. Mention the TTL in your message.
* **C is 2-run gated** — first call returns HELD. Tell the operator "the second confirmation needs to come on the next kaizen pass at 06:00 UTC tomorrow".
* **D is the absolute last resort.** If the operator types "kill all" verbatim, fire `jarvis_kill_switch` with `reason="drawdown_response:{summary}"`.

## What NOT to do

* Do NOT propose more than 4 actions. Decision fatigue at -3R is real.
* Do NOT recommend deploying new bots ("maybe atr_breakout would catch this regime") — that's offensive thinking in a defensive moment.
* Do NOT save the drawdown number to long-term memory. Save the LESSON, not the number ("CPI day at -3R: trim momentum schools aggressively").

## Memory save

After the operator picks an action and you execute it, save ONE fact:

> subject="drawdown playbook",
> predicate="when {cause_cluster_pattern}",
> object="operator chose option {A|B|C|D}: {short summary}",
> trust_score=0.7

Next time a similar drawdown fires, this skill will recall the playbook and bias toward what worked.
