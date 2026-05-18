# Live Capital Cutover — Operator Runbook

Target window: **Historical planning window ~2026-05-15** (3 days from the
original draft).

> **Historical snapshot note:** This runbook captures an older live-cutover
> planning flow. Before acting on it, check current readiness and launch
> authority surfaces, especially
> `python -m eta_engine.scripts.prop_launch_check --json`, plus the current
> operator/runbook lane used by the VPS-facing ETA runtime.

This historical draft is the operator's step-by-step playbook for the
moment paper-soak ends and real capital begins flowing through ONE
selected bot. The Hermes-JARVIS brain-OS was treated here as already
complete and operating — this runbook is about the live-trading switch
scenario, not a current launch clearance by itself.

The historical draft assumed:

* All 33 MCP tools, 12 skills, 5 cron tasks are live for the cutover scenario; re-check current VPS/runtime truth before acting.
* The 9-layer health check + the pre-flight gate-check both PASS.
* The 12-bot paper soak has produced enough data to pick a winner.

---

## T-minus 24 hours — Bot selection

The single most important decision. Don't shortcut.

### Step 1. Identify candidates

```
Hermes: "kelly"   →  jarvis_kelly_recommend
```

Read the top-5 list. Each row gives:
* `bot_id` — the candidate
* `recommended_size_modifier` — Kelly-derived sizing
* `avg_r` — mean R over 30 days
* `n_trades` — sample size
* `rationale` — one-line summary

**Pick a bot only if:**
* `n_trades ≥ 30` — anything smaller is noise
* `avg_r > 0` — must have positive expectancy
* `recommended_size_modifier ≥ 0.3` — Kelly says it's sizeable
* `min_r > -3R` — no historical disaster trades larger than 3R

### Step 2. Adversarial inspection

For the candidate, ask Hermes to argue against it:

```
"Activate jarvis-adversarial-inspector for {bot_id}"
```

The inspector returns ROBUST / FRAGILE / WRONG. **Only proceed on
ROBUST.** If FRAGILE, the operator either picks a different bot OR
trims the planned sizing by 50%.

### Step 3. Council review (historical high-stakes gate template)

```
"Convene council on selecting {bot_id} for an already-approved live cutover"
```

Three voices: advocate → skeptic → judge. **In that historical template,
only proceed on PROCEED**. On PROCEED-WITH-CAUTION, schedule a 60-min
check-in for after the first live trade in that plan.

### Step 4. Save the decision to memory (historical template)

```
"Remember: I selected {bot_id} for an already-approved live cutover on {date}, with sizing
{recommended_size_modifier}, after kelly + adversarial + council all
green."
```

Locks the decision into long-term operator memory. Recallable later.

---

## Historical T-minus 4 hours — Pre-flight gate check

In that historical cutover sequence, run the exhaustive verification:

```powershell
$env:API_SERVER_KEY = '<your-api-key>'
cd C:\EvolutionaryTradingAlgo
python -m eta_engine.scripts.bridge_preflight
```

Expected output ends in:

```
  VERDICT: READY
```

If you see `READY_WITH_CONCERNS`, read each WARN line and decide if any
materially impacts cutover. If you see `NOT_READY`, **STOP**. Resolve
the blockers before proceeding.

Common blockers + fixes:

| Blocker | Fix |
|---|---|
| tunnel FAIL | Restart `hermes_tunnel.ps1` on desktop |
| gateway FAIL | `ssh forex-vps "schtasks /Run /TN ETA-Hermes-Agent"` |
| llm_latency FAIL/WARN | Check DeepSeek status page; if upstream slow, accept the latency OR delay cutover 1h |
| credential_literal FAIL | The 401 bug — re-run `hermes auth add deepseek --type api-key --api-key <literal>` on VPS |
| write_back FAIL | Restart Hermes gateway; if persists, check `var/eta_engine/state/hermes_overrides.json` permissions |
| memory_backup WARN | Run `schtasks /Run /TN ETA-Hermes-Memory-Backup` on VPS manually once |

---

## T-minus 1 hour — Final review

```
Hermes: "zeus"   →  jarvis_zeus unified snapshot
```

Confirm in the snapshot:
* `fleet_status.tier_counts` — your chosen bot is in ELITE or PRODUCER
* `regime` — note the current regime label; if EUPHORIA or CHAOS,
  consider delaying cutover by a session
* `overrides.size_modifiers` — should be empty (no leftover pins)
* `upcoming_events` — no severity-3 event within 60 min
* `wiring_audit.n_dark` — should be 0 or only known-acceptable modules

If any item raises a flag, **delay the cutover**. The bridge will still
be there tomorrow.

---

## T = 0 — The cutover

Out of scope for this doc: the actual broker-account switch (IBKR Pro
account-level flip from paper to live). That's the operator's call
through their broker UI.

In that historical cutover sequence, after the broker is live and the
supervisor is configured for the chosen bot:

### Immediately in that historical cutover:

1. Pin the bot's size to Kelly recommendation:
```
"Set size_modifier for {bot_id} to {recommended_size_modifier} with
reason 'approved live cutover initial sizing', ttl_minutes 1440"
```
24-hour TTL. The override auto-expires tomorrow so you renew or adjust
based on first-day behaviour.

2. Save the cutover event (historical template):
```
"Remember: approved live cutover for {bot_id} executed at {timestamp} with
initial size {modifier}. Reviewing in 4 hours."
```

3. Watch the trace stream:
```
Hermes: "subscribe to consults for {bot_id}"
   →  jarvis_subscribe_events stream=trace
```

---

## T+1 hour — First check-in

```
Hermes: "zeus"
```

Look at:
* `recent_consults` — has the bot consulted yet?
* `attribution_top` — any closed trades?

If the bot has consulted but not entered: that's fine. Portfolio brain
may have blocked. Check `block_reason` in the trace.

If a trade entered and is open: `recent_consults` should show it. Note
its consult_id.

If a trade closed (won or lost): run `jarvis_explain_verdict` on that
consult_id to understand the cascade reasoning.

---

## T+4 hours — Deeper review

```
Hermes: "daily review"   →  jarvis-daily-review skill
```

Renders the 7-section brief specifically scoped to the live-trading
session. Note in particular:

* `Anomalies` section — anything unexpected?
* `Operator action items` — Hermes's recommended next steps

If everything is normal and the bot is behaving as paper-soak predicted:
**don't change anything**. Resist the urge to tinker.

If something is off:
* Drawdown >1R on first day → "convene council on whether to trim"
* Verdict pattern differs from paper-soak → "run anomaly investigator
  on {bot_id}"
* Cost telemetry shows a spend spike → `jarvis_cost_anomaly` to check

---

## T+24 hours — Decision point

The first 24h is the most informative window. By now you know:
1. Did the bot trade live the way it traded on paper? (most important)
2. Did Hermes/JARVIS infrastructure stay alive?
3. Did slippage / fees / fill quality match expectations?

Decision tree:

| Observation | Action |
|---|---|
| Live behavior matches paper, no infra issues | Renew override at same sizing for another 24h, monitor |
| Live behavior matches paper, but ONE infra hiccup | Note the hiccup, fix the root cause, continue at same sizing |
| Live behavior diverges from paper (slippage/fills) | Trim sizing 50%, run jarvis_kelly_recommend again with the new data, decide next session |
| Live behavior catastrophically diverges | Convene council; likely outcome: revert to paper for another week |
| Infra had ≥2 separate failures | Pause live capital, fix infra, re-run preflight, resume |

---

## Week 1 cadence

For the first week of live trading, operator's daily flow:

```
06:30 UTC — morning_briefing auto-fires (existing)
09:30 ET  — zeus_briefing auto-fires (new)
            → operator reads, decides
            → if the current launch/readiness surfaces still agree, continue the approved plan without ad hoc changes
            → if anything off, drill into specific skill
during the day:
            → optional: "subscribe to consults" for live awareness
            → optional: voice mode ("Hey JARVIS, status?")
15:00 ET  — daily_review auto-fires
20:00 UTC Sun — weekly_review auto-fires (after first full week)
```

After 1 week of that already-approved live run, the historical plan was to decide:
* Continue with same bot/sizing → run weekly_review, save the learnings
* Scale up sizing → use jarvis_kelly_recommend with the week's new data
* Add a second bot → run the full T-minus 24h checklist for the candidate
* Pull back to paper → no shame; the bridge supports both seamlessly

---

## Emergency procedures

### Kill switch

If anything goes catastrophically wrong (broker outage, bot misbehaving,
operator no longer confident):

```
1. Operator types EXACTLY: "kill all"
2. Hermes calls jarvis_kill_switch(reason=..., confirm_phrase="kill all")
3. var/eta_engine/state/jarvis_intel/hermes_state.json gets kill_all=true
4. Supervisor reads this and halts trading
5. Operator manually closes any open positions in the broker UI
```

The kill switch is **non-revocable** without operator intervention.
Trading resumes only after operator clears the file manually OR
acknowledges the kill via the supervisor's reset flow.

### Cost runaway

If `jarvis_cost_anomaly` flags a 10× spike:

```
1. Hermes will surface this in zeus_briefing
2. Operator says "show me cost anomaly details"
3. Hermes calls jarvis_cost_summary and identifies the runaway tool
4. If it's a scheduled task firing too often (pre_event_scanner,
   topology_push, etc), edit ~/.hermes/config.yaml cron AND restart
   ETA-Hermes-Agent task
5. If it's an MCP tool being called in a loop, this is a bridge bug;
   restart the gateway and file an issue
```

### Bridge dies during live trading

```
1. Run hermes_bridge_health to identify the failed layer
2. Most common: VPS Hermes Agent task hangs
   → ssh forex-vps "schtasks /End /TN ETA-Hermes-Agent"
   → ssh forex-vps "schtasks /Run /TN ETA-Hermes-Agent"
   → Auto-restart will pick it up if the End fails
3. Bridge down ≠ trading down. The supervisor + bots run independently.
   Hermes just stops providing the operator view.
4. After bridge recovers, run zeus to verify state didn't drift
```

---

## What you should NOT do during live trading

* Don't apply regime packs in the first week — calibrate against your
  specific bot first
* Don't trust Kelly recommendations for bots with <30 closed trades
* Don't convene councils for decisions smaller than "trim by >50%" —
  council cost adds up
* Don't disable the audit log — it's your liability trail
* Don't run multiple agents (T14 bus) until the single-agent flow is
  stable for at least 2 weeks
* Don't enable Discord/Slack/iMessage until you're confident Telegram
  alone is reliable — fewer channels = fewer failure modes

---

## What you SHOULD do during live trading

* Run zeus first thing every morning
* Save anything surprising to memory as a durable fact
* Run weekly_review every Sunday — it builds the operator playbook over time
* Trim aggressively if drawdown >2R; you can always re-pin tomorrow
* Use jarvis_counterfactual after a loss to ask "what if I'd pinned X"
* Read the audit log monthly for "where did Hermes spend my money"

---

## Pre-cutover sign-off checklist

Print this. Check each box. Don't proceed without all checked.

```
□ Bot selected: ___________________
□ Kelly recommended sizing: ___________
□ Adversarial inspector verdict: ROBUST / FRAGILE / WRONG
□ Council judge: PROCEED / PROCEED-WITH-CAUTION / ABORT / DEFER
□ Pre-flight gate-check: READY / READY-WITH-CONCERNS
□ 9-layer health check: 9/9 PASS
□ Memory backup task registered: yes / no
□ Tunnel uptime > 30 min: yes / no
□ DeepSeek upstream healthy: yes / no
□ No severity-3 event in next 60 min: yes / no
□ Initial size_modifier override applied with 24h TTL: yes / no
□ Operator phone has Telegram notifications enabled: yes / no
□ Operator memory has cutover decision saved as fact: yes / no
□ Operator has cleared the next 4 hours to monitor: yes / no

Historical flow note: even if ALL boxes here are checked, confirm the current
ETA launch/readiness authority surfaces still agree before pulling the
broker-side live switch. If ANY unchecked: hold for next session.
```

---

## After cutover — operator commitment

The bridge does its job. The supervisor does its job. JARVIS does
its job. The operator's job from here is:

1. **Read zeus daily.** 15 seconds.
2. **Resist tinkering.** Override only when you have a reason backed
   by data, not by feeling.
3. **Trust the council.** When it says ABORT, abort. You can override,
   but the audit log shows you went against the council.
4. **Build the playbook over time.** Every saved memory fact compounds.
   By month 3 your weekly reviews surface patterns automatically.

In that historical framing, the brain was built and the cutover path was
treated as straightforward. The hard part — which stayed on the operator —
was remaining disciplined for 90 days.
