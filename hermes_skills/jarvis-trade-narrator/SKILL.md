---
name: jarvis-trade-narrator
version: 1.0.0
description: End-of-week 1-page narrative synthesis of the operator's trade journal — patterns, surprises, suggested adjustments.
tags: [trading, review, weekly, journal]
trigger_phrases:
  - "weekly review"
  - "week recap"
  - "synthesize the week"
  - "what happened this week"
  - "narrate the week"
---

# jarvis-trade-narrator (T10)

Reads the per-day trade journal markdown files written by
`brain.jarvis_v3.trade_narrator.append_to_journal()` and synthesizes
them into a 1-page operator-facing narrative. Fires on the Sunday
evening cron OR on operator demand.

This is the END-OF-WEEK reflection layer. The per-trade paragraphs are
generated deterministically inside the consult loop (cheap, fast,
LLM-free). The WEEKLY synthesis is where Hermes earns its keep —
finding patterns across hundreds of paragraphs that the operator would
miss by reading raw.

## When to invoke

* **Scheduled (recommended)**: a `weekly_review` cron entry in
  `~/.hermes/config.yaml scheduled_tasks` fires Sunday at 20:00 UTC
  (4pm ET — operator's typical Sunday review slot).
* **Manual**: "weekly review", "narrate the week", etc.

## Tool sequence

1. Use `read_file` (or the `fact_store` tool's file read variant if
   available) to load the last 7 days of journal files. Paths:
   ```
   C:\EvolutionaryTradingAlgo\var\eta_engine\state\trade_journal\YYYY-MM-DD.md
   ```
   Skip days that don't exist (weekends, off days).

2. `jarvis_fleet_status` — for the current snapshot context.

3. `fact_store action=search query="weekly review patterns" limit=5` — recall
   any prior weekly-review notes the operator saved.

## Output template

```
═══════════ JARVIS WEEKLY NARRATIVE · week ending {YYYY-MM-DD} ═══════════

This week in three sentences:
  {sentence 1: what happened}
  {sentence 2: what surprised us}
  {sentence 3: what's worth adjusting}

Recurring patterns I noticed:
  • {pattern, e.g. "atr_breakout_mnq tends to lose on Wednesdays"}
  • {pattern}
  • {pattern}

Wins to keep doing:
  • {behavior, e.g. "drawdown_response trim-to-0.5x on FOMC day prevented -2R"}
  • {behavior}

Lessons to fold in next week:
  • {specific change, e.g. "block momentum overlay during 12-2pm ET — 4 of the 5 losing days had a noon momentum trade"}
  • {specific change}

Operator action items:
  • {single-line each, max 3, prefixed with priority HI/MID/LO}

══════════════════════════════════════════════════════════════════════════
```

## Memory save discipline

After the operator reads the narrative, save up to 3 facts:

> subject="weekly pattern:{description}"
> predicate="observed week of {date}"
> object="{the pattern + evidence}"
> trust_score=0.6  (starts moderate; reinforce on repeated weeks)

When the SAME pattern shows up in 3+ weekly reviews, bump
`trust_score=0.9` — the pattern is real and the operator should consider
turning it into a rule or override.

## What NOT to do

* Do NOT include raw paragraph dumps from the journal in the output —
  those are the INPUT, not the output. The narrative compresses 200+
  paragraphs into 7 bullet points.
* Do NOT recommend retiring bots or changing live params in the
  narrative. Those decisions need their own consult flow with proper
  gates. The narrative surfaces patterns; the operator decides what to
  do.
* Do NOT save P&L numbers to memory. Numbers shift; patterns persist.

## Calibration

The first 4 weekly reviews are training data. Operator should:

1. Save patterns that resonate as facts (trust 0.6 → 0.9 over time).
2. Mark patterns that turn out wrong as `fact_feedback action=unhelpful`
   so the holographic memory can train its trust scoring.

By week 5–6, recurring patterns surface automatically in memory recall
during weekday sessions, not just in the weekly narrative.

## Edge cases

* **Empty week** (no trading): respond with `"No trading this week
  (supervisor off / market closed / holiday). Skipping narrative."`
* **Single day**: render the narrative with `(thin sample — only N
  consults this week)` as a footer warning.
* **Fewer than 3 lessons**: don't pad. `"Lessons to fold in: nothing
  notable this week."` is a valid output.

## Why the per-trade narration is template-rendered (not LLM)

The journal paragraphs that feed this synthesis come from
`brain.jarvis_v3.trade_narrator.narrate()` — a pure template function
with zero LLM cost. That's intentional:

* Hundreds of consults/day × $0.01-$0.05 per paragraph LLM call = $$$
* Adds 2-3s latency on the consult hot-path → unacceptable
* Determinism: same record → same paragraph, week over week

The expensive AI step happens ONCE A WEEK on the aggregated journal —
the right place for cross-pattern synthesis that the operator can't
easily do by reading raw paragraphs.
