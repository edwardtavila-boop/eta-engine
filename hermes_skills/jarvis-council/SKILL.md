---
name: jarvis-council
version: 1.0.0
description: Three-voice decision council for high-stakes JARVIS actions (kill_switch, retire_strategy, size_modifier ≤0.3×). Advocate / Skeptic / Judge.
tags: [trading, decision-support, high-stakes, devils-advocate, council]
trigger_phrases:
  - "convene the council"
  - "call a council"
  - "council decision"
  - "stress test this decision"
  - "before I retire"
  - "before I kill"
  - "high stakes"
---

# jarvis-council (T9)

For HIGH-STAKES decisions, route the decision through a 3-voice council
before invoking the destructive tool. The council is NOT a gate — it's
a discipline layer that exposes assumptions and creates a clear record
of WHY a decision was made. The operator always retains the final call.

This skill orchestrates 3 reasoning passes within ONE Hermes session
(no extra MCP tools, no extra LLM providers — just disciplined prompt
sequencing inside Hermes itself):

1. **Advocate** — argues the strongest case FOR the action.
2. **Skeptic** — argues the strongest case AGAINST.
3. **Judge** — given both arguments, renders a PROCEED / ABORT verdict
   with reasoning.

## When to convene

The operator should convene a council BEFORE invoking any of:

| Tool | Why it qualifies |
|---|---|
| `jarvis_kill_switch` | Stops the entire fleet — maximum blast radius |
| `jarvis_retire_strategy` (ELITE or PRODUCER tier bot) | Removes a profitable strategy from production |
| `jarvis_set_size_modifier` with `modifier ≤ 0.3` | Effectively a soft-kill on one bot |
| `jarvis_pin_school_weight` with `weight ≤ 0.5 or ≥ 1.5` | Material strategy regime change |
| Any action under -3R drawdown (panic vs disciplined response is hard to tell in the moment) |

**Trigger phrases** above also activate this skill.

## Required input

Operator describes the decision in plain language. The skill EXTRACTS:

* The action (kill / retire / trim / pin)
* The target (which bot / asset / school)
* The proposed magnitude (modifier value / weight value)
* The operator's stated reason

If any of the above is missing, ask the operator one clarifying
question before convening.

## Council protocol (three reasoning passes)

### Pass 1: Advocate (~60s)

Voice the advocate persona:

> *Advocate role:* You're the senior operator who is RECOMMENDING this
> action. You believe this is the right call. Build the strongest case.
>
> Tools you can pull from: `jarvis_fleet_status`, `jarvis_trace_tail`,
> `jarvis_explain_verdict`, `fact_store action=search` for relevant
> prior decisions.
>
> Output: 3 bullets making the case FOR the action, each backed by
> evidence from the JARVIS tools above (not vibes).

### Pass 2: Skeptic (~60s)

Voice the skeptic persona on the SAME evidence:

> *Skeptic role:* You're an experienced senior who has seen this kind
> of action be wrong before. You're NOT a contrarian — you steel-man
> the case for the OPPOSITE decision using the same evidence the
> advocate cited.
>
> Output: 3 bullets making the strongest case AGAINST, each citing
> evidence. Include any hidden risk (correlation cluster, regime
> mismatch, the bot is in its drawdown window, etc.) the advocate
> didn't address.

### Pass 3: Judge (~30s)

Render the final verdict:

> *Judge role:* You've heard both arguments. You make the call. Your
> job is NOT to split the difference — pick a verdict and own it.
>
> Output the council verdict structure exactly as:

```
═══════════ COUNCIL VERDICT ═══════════

Decision: {action} {target} {magnitude}
Operator stated reason: "{reason}"

The advocate's case (one sentence):
  {summary}

The skeptic's case (one sentence):
  {summary}

JUDGE RULING:
  □ PROCEED  — advocate's case dominates
  □ PROCEED-WITH-CAUTION — proceed but operator should set a check-in
                            timer for 60min to see if skeptic's
                            concern materializes
  □ ABORT     — skeptic's case dominates; the action is premature
                or the evidence doesn't support it
  □ DEFER     — neither case is decisive; gather more data first
                (which specific data: ...)

Judge rationale (2-3 sentences):
  {explicit reasoning citing the bullets above}

If PROCEED: operator's next step is to invoke the actual tool
({tool_name}) with the exact args the council reviewed.

If PROCEED-WITH-CAUTION: same as PROCEED but operator schedules a
60-min check by saying "council check on {target}".

If ABORT or DEFER: operator does NOT invoke the tool. The reason for
the abort/defer is the new fact to memorize.

═══════════════════════════════════════════
```

## Discipline rules

* **One council per decision.** Don't re-convene to overturn an ABORT.
  If the operator strongly disagrees with ABORT, that's a fact about
  operator conviction — log it via `fact_store` so future councils
  can see it.
* **Evidence-only.** Each pass cites JARVIS tool output, fact_store
  recalls, or specific consult records. NEVER hypothetical concerns
  unrelated to the trace.
* **Steel-man both sides.** Weak advocate = wasted council. Weak
  skeptic = wasted council.
* **Judge picks decisively.** "It depends" is not a council verdict.
  If the judge truly can't decide, the verdict is DEFER with explicit
  data requirements.

## Memory save (always)

After the council, save ONE durable fact:

> subject="council:{decision_type}:{target}"
> predicate="convened on"
> object="{date}: judge ruled {RULING}; advocate argued {bullet 1};
>          skeptic argued {bullet 1}"
> trust_score=0.7

Builds a per-decision-type playbook. After 5+ councils on the same
decision type, recurring patterns surface in advocate/skeptic
arguments → bias future councils toward the operator's actual playbook.

## Cost

3 chat completions per council ≈ 3 × $0.05 = $0.15/council at current
DeepSeek-V4-Pro pricing. Cheap for the discipline it provides.

Expected frequency: ~1-3 councils/week (only on actual high-stakes
decisions). Monthly council cost: < $5.

## What the council does NOT do

* The council does NOT execute the destructive tool. The operator
  invokes the tool manually after PROCEED.
* The council does NOT have veto power. The operator can override
  ABORT — but the audit log will show the council ruled ABORT and the
  operator proceeded anyway. That's an accountability trail, not a
  block.
* The council is NOT a substitute for the 2-run gate on
  `jarvis_retire_strategy`. Both apply: council first (discipline),
  then 2-run gate (technical safety).

## Edge cases

* **Kaizen-recommended action** (e.g. RETIRE flagged by autonomous
  kaizen loop): convene the council BEFORE confirming the kaizen
  recommendation. The council often catches "this regime is hostile
  to this strategy this week, but kaizen looked at 30 days and missed
  the regime context".
* **Operator stressed / mid-drawdown**: this is exactly when the
  council is most valuable. Force the discipline; don't let panic
  bypass it.
* **Multiple simultaneous decisions**: one council per decision.
  If the operator is making 3 trims at once, that's 3 councils. Don't
  bundle.

## Operator playbook

Standard workflow for a high-stakes call:

1. Operator: "I want to retire eth_perp. Sharpe -1.6 for two weeks."
2. Hermes: activates this skill, runs advocate → skeptic → judge.
3. Council judge rules PROCEED-WITH-CAUTION (operator's case is strong
   but the skeptic noted that ETH had a regime shift 3 days ago that
   could have explained the dip).
4. Operator: "OK, proceed. Set the check-in timer."
5. Hermes: invokes `jarvis_retire_strategy(bot_id="eth_perp",
   reason="Sharpe -1.6 over 2 weeks, council PROCEED-WITH-CAUTION")`.
6. Tool returns HELD (2-run gate). Operator confirms on the next
   kaizen pass.
7. 60min later: Hermes pings operator: "Council check on eth_perp.
   Want to review the skeptic's concern about the ETH regime shift?"

This is what supercharged decision discipline looks like.
