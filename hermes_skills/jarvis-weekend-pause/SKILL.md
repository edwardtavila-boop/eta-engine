---
name: jarvis-weekend-pause
version: 1.0.0
description: Friday-close / Sunday-open workflow. Pauses all bots before weekend, schedules Sunday-evening re-activation. Operator-confirmed at both gates.
tags: [trading, pause, weekend, session-management]
trigger_phrases:
  - "pause for weekend"
  - "weekend pause"
  - "pause everything"
  - "shut down for weekend"
  - "Friday close routine"
  - "resume from weekend"
  - "Sunday open"
  - "back from weekend"
---

# jarvis-weekend-pause — Friday-Close to Sunday-Open

Operator's clean weekend routine. Two modes:

* **PAUSE mode**: Friday near market close → trim every live bot to 0.0
  with a TTL that lands at Sunday evening. Trading stops; capital sits.
* **RESUME mode**: Sunday evening → clear the pauses + run preflight +
  zeus brief to confirm everything is ready for Monday open.

The pause is implemented as TTL-bounded size_modifier overrides
(0.0× = effectively no sizing → no trade entries). When the TTL
expires, the bots resume automatically. This means: **even if the
operator forgets to run RESUME, the bots come back online on schedule.**

## When to invoke

* **PAUSE**: Friday 3:30-4:00 PM ET, OR anytime operator wants to step
  away (vacation, sick day, "I need a clean weekend").
* **RESUME**: Sunday 6-8 PM ET before futures globex re-opens, OR
  anytime operator wants to verify the auto-resume worked.

## Required inputs

* Mode (auto-detected from trigger phrase): PAUSE or RESUME
* (optional) `bot_filter` — pause/resume only specific bot_ids
  (default: all bots in current fleet)
* (PAUSE only) `resume_at_iso` — when the TTL expires; default
  "next Sunday 22:00 UTC" = ~6pm ET

## PAUSE workflow

```
Step 1: GET CURRENT FLEET
   ├─ call jarvis_zeus (force_refresh=true)
   ├─ from fleet_status, extract list of all bot_ids
   │   (use top5_elite, top5_dark, and any bots in actions list)
   └─ filter by bot_filter if operator supplied one

Step 2: CONFIRM WITH OPERATOR
   ├─ "I'm about to pause {N} bots until {resume_at_iso}:"
   ├─ list the bot_ids
   ├─ "Each will get a size_modifier=0.0 override with TTL aligned
   │  to that resume time. The pause will auto-expire even if you
   │  don't actively resume."
   └─ "Confirm? (yes/no)"

Step 3: APPLY PAUSE OVERRIDES
   ├─ compute ttl_minutes = (resume_at - now) in minutes
   ├─ for each bot_id:
   │   call jarvis_set_size_modifier(
   │     bot_id={bot_id},
   │     modifier=0.0,           # no sizing = no entry
   │     reason=f"weekend pause until {resume_at_iso}",
   │     ttl_minutes={ttl_minutes},
   │   )
   ├─ collect successes + failures
   └─ if any failed, warn but continue (operator can retry individuals)

Step 4: MEMORY ANCHOR
   ├─ save fact:
   │   subject="weekend pause"
   │   predicate="initiated on"
   │   object=f"{now}: paused {N} bots until {resume_at_iso}"
   │   trust_score=0.6
   └─ short trust because this is operational state, not durable insight

Step 5: REPORT
```

### PAUSE output template

```
═══════════ WEEKEND PAUSE · {now} ═══════════

Paused:  {N} bot(s)
Resume:  {resume_at_iso}  (auto via TTL)

Bots paused:
  {bot_a}  0.0× for {N}h
  {bot_b}  0.0× for {N}h
  ...

What happens now:
  • Each bot's portfolio_brain.assess multiplies its base size
    by 0.0 → no new entries fire until the override expires.
  • Existing open positions are NOT closed. Operator should close
    those manually in the broker UI if they want a clean weekend.
  • The TTL is a hard floor: even if Hermes goes down, the override
    expires automatically when its expires_at timestamp passes.

To resume early (e.g. Sunday morning instead of evening):
  say "resume from weekend" — this skill clears all overrides
  with reason starting "weekend pause".

═══════════════════════════════════════════════════════════
```

## RESUME workflow

```
Step 1: FIND ACTIVE WEEKEND-PAUSE OVERRIDES
   ├─ call jarvis_active_overrides
   ├─ filter size_modifiers where reason starts with "weekend pause"
   └─ if none: "No active weekend-pause overrides found.
       Either they already auto-expired, or you didn't pause."

Step 2: CONFIRM WITH OPERATOR
   ├─ "I'm about to clear {N} weekend-pause overrides:"
   ├─ list the bot_ids
   └─ "Confirm? (yes/no)"

Step 3: CLEAR EACH OVERRIDE
   ├─ for each bot_id:
   │   call jarvis_clear_override(bot_id={bot_id})
   └─ collect successes + failures

Step 4: PRE-RESUME PREFLIGHT
   ├─ remind operator: "Run `python -m eta_engine.scripts.bridge_preflight`
   │  to confirm VERDICT: READY before market open."
   └─ skill doesn't run it automatically (preflight is on operator's
       desktop, not on Hermes)

Step 5: ZEUS BRIEF
   ├─ call jarvis_zeus(force_refresh=true)
   └─ render the 9-section operator-friendly output as the
       "we're back" report

Step 6: MEMORY ANCHOR
   ├─ save fact:
   │   subject="weekend resume"
   │   predicate="completed on"
   │   object=f"{now}: cleared {N} pause overrides, fleet ready"
   │   trust_score=0.6
   └─ this builds the operator's weekend-routine track record
```

### RESUME output template

```
═══════════ WEEKEND RESUME · {now} ═══════════

Cleared:  {N} weekend-pause override(s)

Bots back online:
  {bot_a}  ← cleared, will trade on next consult
  {bot_b}  ← cleared
  ...

Pre-open checklist:
  □ Run `python -m eta_engine.scripts.bridge_preflight`
  □ Confirm VERDICT: READY
  □ Open Hermes-desktop and read this morning's Zeus snapshot

{zeus_summary_inline}

═══════════════════════════════════════════════════════════
```

## Discipline rules

* **Never call kill_switch as part of weekend pause.** kill_switch is
  for emergencies; pause is for routine downtime. Size_modifier=0 is
  the right tool.
* **Never close open positions automatically.** That's the operator's
  call. The skill flags the open positions in its output so the
  operator can decide.
* **TTL must be a hard floor.** Calculate ttl_minutes precisely;
  err on the LATE side (longer pause) by 30 min to avoid Sunday
  evening surprises.
* **Always require explicit confirmation.** Both PAUSE and RESUME
  require an explicit yes before applying. No silent application.

## Edge cases

* **Operator runs PAUSE when overrides already exist**: warn that
  existing pins will be OVERWRITTEN. Ask if operator wants to merge
  or replace.
* **Operator runs RESUME when no pause-overrides exist**: report
  "auto-expired or never paused" and skip the clear step. Still run
  preflight + zeus.
* **TTL ends at exactly 22:00 UTC Sunday**: that's the default; works
  for US futures Globex open at 22:00 UTC Sun. For crypto bots that
  trade 24/7, the operator can specify a different resume_at_iso.
* **Resume time falls on Monday holiday**: skill doesn't check market
  calendars (not its job). Operator is responsible for setting the
  right resume time.

## Memory save discipline

Both PAUSE and RESUME write durable facts with trust_score=0.6
(moderate). After a few weeks of recurring weekend pauses, recall
patterns surface in weekly_review:
* "Operator routinely pauses Fri 3:45 ET"
* "Operator has paused 6 out of last 8 weekends"
* "Pause durations average 56 hours"

These observations feed into future zeus snapshots ("you usually pause
in 30 min — heads up?").

## Cost

PAUSE: 1-2 chat completions (fleet enumeration, override application)
≈ $0.10. RESUME: 2-3 (preflight reminder, override clear, zeus brief)
≈ $0.15. Total weekend routine cost: ~$0.25.

## Why this is a skill, not just a CLI script

The pause/resume could be a CLI tool, but the SKILL form gives the
operator natural-language access ("Hey JARVIS, pause for weekend").
The skill also enforces the confirm-gates that a CLI would skip, and
the memory-anchor builds operational history over time.
