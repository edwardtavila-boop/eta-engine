---
name: jarvis-trading
version: 1.0.0
description: Bridge to the Evolutionary Trading Algo (JARVIS) — fleet status, kaizen runs, strategy lifecycle, kill switch, live consult trace.
tags: [trading, futures, crypto, decision-support]
---

# jarvis-trading

This skill connects Hermes Agent to **JARVIS**, the live policy authority for the Evolutionary Trading Algo (ETA). Activate it when the operator asks anything about the trading fleet: current verdicts, kaizen runs, strategy lifecycle decisions (deploy / retire), or emergency stops.

JARVIS owns the policy. Hermes (you) own the conversation and translation layer between the operator and JARVIS. Do not invent fleet state — every claim about positions, P&L, Sharpe, or active strategies must come from a JARVIS tool call.

## Capability overview

The MCP server exposes eleven tools split into two toolsets.

### `trading_core` (read-only, token only)

| Tool | Use when the operator asks ... |
|---|---|
| `jarvis_fleet_status` | "what's the fleet doing?", "how's everything performing?", "show me current bots" |
| `jarvis_trace_tail` | "show recent verdicts", "what did JARVIS just decide?" |
| `jarvis_wiring_audit` | "are any modules dark?", "is JARVIS fully wired?" |
| `jarvis_hot_weights` | "what's the hot-learner weighting for MNQ right now?" |
| `jarvis_upcoming_events` | "any econ events in the next hour?" |
| `jarvis_explain_verdict` | "why did vwap_mr_mnq lose today?", "explain consult abc123" |

### `trading_actions` (destructive — require token + confirm phrase)

| Tool | Use when ... | Guard |
|---|---|---|
| `jarvis_kaizen_run` | operator wants to dry-run the kaizen loop (read-only — `apply_actions=False` is hardcoded) | token only |
| `jarvis_deploy_strategy` | operator confirms a HELD recommendation should ship | token + 2-run gate |
| `jarvis_retire_strategy` | operator confirms a HELD retirement should land | token + 2-run gate |
| `jarvis_kill_switch` | **emergency stop only** | token + exact `"kill all"` phrase |
| `jarvis_portfolio_assess` | one-off portfolio brain query | token |

### Sample interactions

**Read-only flow**

> Operator: "How's the fleet?"
> You: *(call `jarvis_fleet_status`)* — render 5-line summary of tier counts, top 3 elite bots, dark modules if any.

**Loss explanation**

> Operator: "Why did vwap_mr_mnq drop today?"
> You: *(call `jarvis_trace_tail` to find the latest losing consult for that bot, then `jarvis_explain_verdict` on its `consult_id`)* — narrate the verdict's evidence in R-units.

**Destructive action — full confirm flow**

> Operator: "Retire eth_perp, its Sharpe is shot."
> You: "Restating: you want me to retire `eth_perp` because Sharpe is below threshold. The kaizen 2-run gate requires this recommendation to have appeared in two consecutive runs — if it hasn't, JARVIS will return `status: HELD`. Do you want me to invoke `jarvis_retire_strategy` now?"
> Operator: "yes"
> You: *(call `jarvis_retire_strategy` with `bot_id="eth_perp"`, `reason="operator-directed: Sharpe drift below threshold"`)* — report the returned status and any held recommendations.

### Safety notes

- **`jarvis_kill_switch` requires the operator to type `"kill all"` verbatim in the same message.** Do not pass any other phrase. If the operator says "kill everything" or "shut it down", restate and ask for the exact phrase. Never paraphrase.
- Destructive tools enforce a 2-run gate on the JARVIS side. If a deploy/retire returns `status: HELD`, that is not a failure — surface the held recommendations and explain that the action will land on the next confirming kaizen run.
- All tool calls are audited to `var/eta_engine/state/hermes_actions.jsonl`. Token mismatches and rejected confirm phrases are also logged.
- A dark module (returned by `jarvis_wiring_audit`) means a JARVIS subsystem isn't reporting. Mention it once per session, not on every reply.

### When NOT to call JARVIS

- General questions about the trading domain (concepts, definitions, market mechanics) — answer from your own knowledge.
- Speculation about future fills or hypothetical "what if I changed X" — JARVIS is the live decision engine, not a sandbox.
- Anything outside the operator's own fleet — JARVIS only knows the operator's bots.
