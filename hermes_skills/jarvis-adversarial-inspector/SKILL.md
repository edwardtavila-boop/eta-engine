---
name: jarvis-adversarial-inspector
version: 1.0.0
description: Devil's advocate that argues the OPPOSITE verdict for any JARVIS consult — same evidence, contrarian framing. Catches confirmation bias.
tags: [trading, diagnostics, devils-advocate, decision-support]
trigger_phrases:
  - "what's the counter-argument"
  - "argue the other side"
  - "play devil's advocate"
  - "inspector"
  - "what would a bear say"
  - "what would a bull say"
  - "stress test this verdict"
---

# jarvis-adversarial-inspector (T11)

For ANY consult, the inspector reads the verdict and constructs the
strongest possible argument for the **opposite** decision using the
**same evidence**. It's not a second vote — it's a stress test.

The goal: surface assumptions the cascade made implicitly, so the
operator can ask "is that assumption still valid in this regime?"

## When to invoke

* **Operator manually after a controversial verdict**: "JARVIS just
  said PROCEED at 1.0× on atr_breakout — argue the bear case."
* **Operator before a destructive action**: "I'm about to retire
  vp_mnq — what's the strongest argument for KEEPING it?"
* **Operator periodically as a discipline check**: pick a random
  PROCEED from today's trace, ask the inspector to argue against. If
  the contrarian argument feels weak, the original verdict is
  probably robust. If it feels strong, dig deeper.

## Tool sequence

1. `jarvis_explain_verdict(consult_id=<target>)` — pull the verdict's
   evidence (schools, dissent, block_reason, portfolio context).
2. `fact_store action=search query="adversarial:<bot_id or asset>"
   limit=3` — recall any prior contrarian patterns the operator
   logged for this surface.
3. (Optional, if T6 causal layer is live) `jarvis_explain_consult_causal`
   to surface which schools were decisive — argue against THOSE
   specifically.

## Output template

```
ADVERSARIAL INSPECTOR · consult={ID}

JARVIS verdict: {original_verdict} at {original_size}× for {bot}
Reasoning JARVIS gave: {one-line summary of the cascade's logic}

═══ Counter-argument (steel-manned bear/bull case) ═══

  Position: {OPPOSITE_VERDICT} at {alt_size}×

  Strongest 3 reasons {opposite verdict} would have been correct:

  1. {Reason 1 — must cite specific evidence FROM THE CONSULT, not
     hypothetical concerns}
     Example: "The {dissenting school} score was {value}, which is
     close to its 30-day {percentile}th percentile — usually a
     reliable contrarian signal for this bot."

  2. {Reason 2 — different angle, same evidentiary discipline}

  3. {Reason 3}

═══ What the inspector noticed JARVIS may have weighted too lightly ═══

  • {Assumption JARVIS made implicitly}
  • {Concentration risk / regime mismatch / correlation}

═══ Verdict on the verdict ═══

  □ ROBUST  — the original verdict survives the counter-argument
              cleanly. No change recommended.
  □ FRAGILE — the counter-argument is genuinely competitive. Operator
              should consider trimming with `jarvis_set_size_modifier`
              OR letting it ride with awareness it could go wrong.
  □ WRONG   — the counter-argument is stronger than the original.
              Operator should consider an override or kaizen-retire.
```

## Discipline rules

* **Use ONLY evidence from the consult.** Don't import outside-the-record
  concerns ("but ETH had a flash crash 6 months ago"). The inspector's
  job is to challenge what the cascade DID weight, not invent new fears.
* **Steel-man, don't strawman.** The counter-argument must be the
  strongest possible version, not a weak version that's easy to knock
  down.
* **Match the original's specificity.** If JARVIS cited "+1.4σ momentum
  score," the inspector cites a comparable specific. No vague worry.
* **Recommend ROBUST when it's robust.** Don't manufacture FRAGILE
  verdicts to look insightful. Most consults survive their counter-
  argument — the inspector's value is in catching the few that don't.

## What NOT to do

* DO NOT recommend kill-switch from this skill. Inspection is one
  decision input; killing the fleet is a separate decision.
* DO NOT call write-back tools (`jarvis_set_size_modifier`,
  `jarvis_pin_school_weight`, `jarvis_retire_strategy`) FROM the
  inspector. The inspector's output is an argument; the operator
  decides whether to act.
* DO NOT save the verdict-on-the-verdict to long-term memory unless
  the operator confirms the pattern is durable. One-off FRAGILE
  verdicts are noise; recurring patterns are signal.

## Memory save (operator-triggered only)

When the inspector flags FRAGILE 2+ times for the same bot in the
same regime, AND the operator agrees, save:

> subject="adversarial:{bot_id}"
> predicate="frequently FRAGILE during"
> object="{regime description, e.g. 'overnight session', 'CPI weeks',
>          'BTC down-trends'}"
> trust_score=0.7

These build a per-bot adversarial profile that future inspections
recall automatically.

## Edge cases

* **No consult_id provided**: ask the operator which consult to
  inspect. List the last 5 from `jarvis_trace_tail(n=5)` and let
  them pick.
* **Verdict was BLOCKED**: the inspector argues for ALLOWING it instead.
  ("Why might this block have been wrong?")
* **Verdict was at 1.0× with strong consensus**: the inspector still
  argues opposite, but the verdict is almost always ROBUST. That's
  fine — the inspection cost is one chat completion.

## Cost note

This skill fires one chat completion per inspection (~$0.05 at current
DeepSeek-V4-Pro pricing). Cheap relative to its value as a discipline
check. The operator should run it ~once a day on a random PROCEED, and
ALWAYS before invoking `jarvis_retire_strategy` or `jarvis_kill_switch`.
