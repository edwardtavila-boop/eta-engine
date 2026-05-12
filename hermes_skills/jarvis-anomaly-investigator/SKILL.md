---
name: jarvis-anomaly-investigator
version: 1.0.0
description: Cluster-of-losses diagnostician — fires when one bot or one school has ≥5 consecutive losing consults. Distinguishes regime change from broken-strategy.
tags: [trading, diagnostics, anomaly, automatic]
trigger_phrases:
  - "anomaly"
  - "5 losses in a row"
  - "losing streak"
  - "what's wrong with"
  - "stopped working"
auto_trigger:
  event_stream: trace
  condition: "bot_loss_streak >= 5 OR school_loss_streak >= 5"
  rate_limit_minutes: 60
---

# jarvis-anomaly-investigator

Triggered when a single bot OR a single school accumulates 5+ consecutive losing consults. Differentiates between:

* **Regime change** — the world changed; the strategy is sound but its edge evaporated for now.
* **Broken strategy** — the strategy has stopped producing valid edge entirely; retire candidate.

## When to invoke

* **Manual today**: operator says "what's wrong with vwap_mr_mnq", "vp_mnq stopped working", "anomaly in BTC bots", etc. Hermes recognizes trigger phrases and activates.
* **Auto-trigger (aspirational — not yet wired)**: the `auto_trigger:` frontmatter declares the intended condition. A future conductor (T14 inter-agent bus or a dedicated streak-watcher) will poll `jarvis_subscribe_events(stream=trace)` for `block_reason=None AND verdict.final_verdict in ("EXIT","STOP_LOSS")` clustered around one bot_id, threshold 5 consecutive losses in 24h. Until that lands, manual invocation only.

## Two-phase investigation

### Phase 1: Pattern recognition (read-only)

1. `jarvis_trace_tail(n=200)` — wide window to find the streak.
2. Filter to consults with `bot_id=<target>`. Identify the losing run.
3. `jarvis_explain_verdict(consult_id=<each loss>)` for the most recent 5 losses — collect block_reasons and dissent patterns.
4. `jarvis_hot_weights(asset=<target_asset>)` — has the EMA shifted hard against this bot's school?
5. `fact_store action=search query="anomaly {bot_id}"` — has this happened before? What did the operator do?

### Phase 2: Diagnosis output

```
ANOMALY INVESTIGATION · {bot_id} · {n} consecutive losses

Loss pattern:
  → {timestamp}: action={action}, dissent={schools that dissented}
  → ... (last 5)

Common factor:
  {block_reason or "dissent from {school_x} on all 5 losses" or "fleet drawdown coincident"}

Hot-learner state:
  {school}={weight}  →  the EMA learner has {moved against | not yet reacted to} this streak

Historical context:
  {recalled facts or "no prior anomaly recorded for this bot"}

Diagnosis: {one of three}
  □ REGIME CHANGE — strategy sound, market hostile. Action: TRIM via override (TTL 240m).
  □ BROKEN STRATEGY — edge gone, retire candidate. Action: HELD retire via kaizen.
  □ COINCIDENT — losses coincided with fleet drawdown / event; strategy probably OK. Action: WATCH.

Recommended next step: {single sentence}
```

## Decision heuristic

* **All 5 losses share the same `block_reason`** (e.g. "correlation_cluster_high") → COINCIDENT — strategy was correctly blocked, system worked.
* **All 5 losses had dissent from the same school** → BROKEN STRATEGY — the school the bot relies on has lost predictive power.
* **Hot-learner weight against the bot's primary school has drifted >0.3 below 1.0** → REGIME CHANGE — the EMA learner is already adjusting; help it along.
* **Losses are spread across uncorrelated block_reasons** → BROKEN STRATEGY (most likely) — the strategy is making random bad calls.

## Action policy

* **REGIME CHANGE** → propose `jarvis_set_size_modifier(modifier=0.3, ttl_minutes=240)` and ask operator to approve.
* **BROKEN STRATEGY** → propose `jarvis_retire_strategy` — note this is 2-run gated, so the first call returns HELD.
* **COINCIDENT** → recommend `WATCH` — no action needed. Confirm with operator that no override is desired.

## Memory save (always)

> subject="anomaly:{bot_id}",
> predicate="diagnosed as",
> object="{REGIME|BROKEN|COINCIDENT} at {timestamp}; operator action: {action taken or 'WATCH'}",
> trust_score=0.7

This builds a per-bot anomaly history. Future investigations recall it via `fact_store action=related` to surface patterns ("this bot has had 3 REGIME diagnoses in 30 days — maybe the strategy doesn't fit current market structure").
