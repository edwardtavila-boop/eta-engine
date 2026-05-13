---
name: jarvis-onboarding
version: 1.0.0
description: Paper-to-live bot promotion workflow. Walks operator through Kelly + adversarial + council + sign-off before activating a candidate bot for real capital.
tags: [trading, onboarding, cutover, promotion, kelly]
trigger_phrases:
  - "promote bot"
  - "add bot to live"
  - "onboard bot"
  - "graduate bot"
  - "go live with"
  - "switch bot to live"
---

# jarvis-onboarding — Paper-to-Live Bot Promotion

Operator says "promote atr_breakout_mnq to live" or any of the trigger
phrases. This skill walks through the complete checklist defined in
`LIVE_CUTOVER_OPERATOR_RUNBOOK.md` for that ONE bot — Kelly sizing,
adversarial inspection, council review, sign-off recording.

This is the operator's "graduation ceremony" for a paper bot. After
this skill runs successfully, the bot is sized + memory-tagged +
override-pinned and ready for the broker-side live switch.

## When to invoke

* Operator wants to add a new bot to live capital
* Operator wants to swap from bot A to bot B
* End of any paper-soak window (the operator's recurring "weekly review
  → promote winners" cadence)

## Required inputs

The operator supplies:

* `bot_id` — exact bot identifier (must match a `jarvis_kelly_recommend`
  row with `insufficient_data=False`)
* (optional) `kelly_fraction` — defaults to 0.25 (quarter-Kelly); operator
  can override to 0.5 / 0.10 / etc.

If the operator says "promote atr_breakout_mnq" with no other context,
proceed with defaults and ask for confirmation at each gate.

## Full workflow

```
Step 1: KELLY CHECK
   ├─ call jarvis_kelly_recommend
   ├─ find the row for bot_id
   ├─ if insufficient_data: ABORT with "need ≥ 20 trades in lookback window"
   ├─ if avg_r ≤ 0: ABORT with "no positive expectancy"
   ├─ if recommended_size_modifier < 0.2: WARN
   └─ note: recommended_size_modifier, avg_r, std_r, min_r, n_trades

Step 2: ADVERSARIAL INSPECTION
   ├─ activate jarvis-adversarial-inspector for bot_id
   ├─ render the bear case
   └─ get operator's read: ROBUST / FRAGILE / WRONG
       ├─ ROBUST: proceed
       ├─ FRAGILE: ask operator if they want to trim Kelly recommendation by 50%
       └─ WRONG: ABORT with "adversarial inspector says the bear case
                          dominates; reconsider the bot"

Step 3: COUNCIL REVIEW
   ├─ activate jarvis-council
   ├─ scope: "promote {bot_id} to live with size_modifier={recommended}"
   ├─ run advocate → skeptic → judge passes
   └─ get verdict: PROCEED / PROCEED-WITH-CAUTION / ABORT / DEFER
       ├─ PROCEED: continue
       ├─ PROCEED-WITH-CAUTION: continue but schedule a 60-min check-in
       ├─ ABORT: stop and explain
       └─ DEFER: stop and tell operator what data to gather

Step 4: PRE-FLIGHT CHECK
   ├─ note: this skill DOESN'T run preflight itself — operator should
   │  run `python -m eta_engine.scripts.bridge_preflight` manually
   ├─ remind: "Have you run bridge_preflight and seen VERDICT: READY?"
   └─ if no: ask operator to confirm they will before broker-side switch

Step 5: APPLY INITIAL OVERRIDE
   ├─ call jarvis_set_size_modifier(
   │    bot_id={bot_id},
   │    modifier={kelly_recommended_or_trimmed},
   │    reason="live cutover initial sizing — onboarding skill",
   │    ttl_minutes=1440  # 24 hours
   │  )
   └─ confirm APPLIED status

Step 6: MEMORY ANCHOR
   ├─ call fact_store action=add with:
   │    subject=f"live cutover:{bot_id}"
   │    predicate="executed on"
   │    object=f"{date}: kelly={X}, council={verdict}, initial_size={Y}"
   │    trust_score=0.9
   └─ this is the operator's audit trail in long-term memory

Step 7: OPERATOR FINAL SAY
   ├─ summarize: "I've staged everything for {bot_id} live cutover:
   │    - Kelly says size at {Y}×
   │    - Inspector said: {ROBUST|FRAGILE|WRONG}
   │    - Council said: {verdict}
   │    - Override is APPLIED with 24h TTL
   │    - Memory anchor saved
   │    Now go to your broker UI and flip {bot_id} from paper to live."
   └─ DO NOT call broker tools. The broker-side switch is operator-only.
```

## Output template

After all 7 steps complete:

```
═══════════ ONBOARDING COMPLETE · {bot_id} ═══════════

Bot:               {bot_id}
Kelly sizing:      {recommended_size_modifier}× (avg_r={avg_r}, n_trades={n_trades})
Inspector verdict: {ROBUST | FRAGILE | WRONG}
Council judge:     {PROCEED | PROCEED-WITH-CAUTION | ABORT | DEFER}
Override applied:  yes ({modifier}× for 24h)
Memory anchored:   yes

NEXT STEP (operator-only):
  Open your broker UI (IBKR Pro / Tastytrade / whichever).
  Flip {bot_id}'s account binding from paper to live.
  Run `python -m eta_engine.scripts.bridge_preflight` ONE MORE TIME
  immediately before the flip — VERDICT must be READY.

Schedule your T+1h check-in:
  At {now + 1h}, say "zeus" to see how the bot is behaving.

═══════════════════════════════════════════════════════════
```

## Discipline rules

* **NEVER call broker tools.** This skill doesn't touch live trading
  state directly. It prepares the brain-OS side; the operator flips
  the broker switch.
* **NEVER skip a step.** Even if the operator says "I trust this bot,
  skip the council" — politely refuse. The discipline is the point.
* **NEVER apply a size_modifier > Kelly recommendation.** If the
  operator wants to go higher, they manually call jarvis_set_size_modifier
  after this skill exits. This skill caps at Kelly.
* **DEFAULT to quarter-Kelly.** Quarter-Kelly is the standard
  operator's prior. Going higher requires explicit operator override.

## What this skill does NOT do

* It does NOT promote a bot that has < 20 trades in the lookback window
  (insufficient_data check).
* It does NOT push the bot to live on the broker. Operator only.
* It does NOT modify per_bot_registry.py or the supervisor config.
  Those are separate concerns owned by the operator's deploy flow.
* It does NOT enable a bot that's currently in the kaizen_overrides
  deactivated list. Operator must clear that first via
  `jarvis_deploy_strategy` (2-run gated).

## Memory save

This skill ALWAYS saves a durable fact at Step 6:

```
subject="live cutover:{bot_id}"
predicate="executed on"
object="{ISO date}: kelly={X}, inspector={Y}, council={Z},
         initial_size={W}, ttl=24h"
trust_score=0.9
```

This becomes the audit trail. Weeks later, "what was my reasoning
when I went live with atr_breakout_mnq?" is answered by
`fact_store action=search query="live cutover atr_breakout_mnq"`.

## Common operator questions during the workflow

| Operator asks | Skill response |
|---|---|
| "Can I size higher than Kelly?" | "Not through this skill. Run the workflow at Kelly, then manually pin higher after if you really want — but the audit log will show you went above the recommendation." |
| "Kelly is < 0.2× — is the bot good enough?" | "Marginal. The conservative path is to hold for more paper data. The aggressive path is to live at 0.2× and see if real-fill quality matches paper." |
| "Council ruled ABORT — can I override?" | "Yes, but I won't help. The override path is to manually call jarvis_set_size_modifier yourself. The council's ABORT will be in the audit log." |
| "Inspector said FRAGILE — should I trim?" | "Standard play: trim Kelly recommendation by 50%. Council still has the final say." |

## Cost

This skill fires 3-4 chat completions (kelly summary, inspector,
council 3-pass, final synthesis) = ~$0.20 per onboarding. Cheap for
the discipline.

## Edge cases

* **Bot already has an active size_modifier**: warn and ask operator
  to clear the existing pin first via `jarvis_clear_override` (avoids
  TTL extension surprises).
* **Operator wants to onboard multiple bots in one session**: run this
  skill once per bot. Don't bundle — council discipline degrades.
* **Live cutover happens outside trading hours**: that's fine. The
  override applies at the next consult; no immediate trade fires.
