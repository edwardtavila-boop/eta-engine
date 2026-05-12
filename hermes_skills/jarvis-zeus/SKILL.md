---
name: jarvis-zeus
version: 1.0.0
description: ZEUS SUPERCHARGE — the operator's one-call situational awareness. Unifies all 17 tracks into a single command-center view. "Hermes, what's happening?"
tags: [trading, zeus, supercharge, command-center, situational-awareness]
trigger_phrases:
  - "zeus"
  - "status"
  - "what's happening"
  - "show me everything"
  - "command center"
  - "situational awareness"
  - "where are we"
  - "fleet briefing"
---

# jarvis-zeus — The Unified Brain (Zeus Supercharge)

After 17 tracks of building specialized lenses, Zeus is the single
entry point. Operator says any trigger above; Hermes activates this
skill; one tool call (`jarvis_zeus`) returns the complete state across
every brain surface.

This is the **command-center view**. The operator's morning routine:
"Zeus" → 15 seconds of reading → knows everything material.

## Tool sequence

Just ONE call:

```
jarvis_zeus(force_refresh=False)
```

That returns:

```python
{
  "asof": ISO timestamp,
  "fleet_status": {...},        # tier counts, top elite/dark
  "topology": {...},             # n_nodes, n_edges
  "overrides": {...},            # active size_modifiers + school_weights
  "regime": {...},               # current market regime + recommended pack
  "recent_consults": [...],      # last 10 consults
  "kelly_recs": [...],           # top-5 Kelly recommendations
  "attribution_top": {...},      # top winners/losers last 7 days
  "sentiment": {...},            # BTC + ETH fear/greed
  "wiring_audit": {...},         # dark modules
  "upcoming_events": [...],      # events ≤ 60 min ahead
  "bots_online": [...],          # registered inter-agent bus members
  "cache_age_s": float
}
```

## Output template

```
═══════════ ZEUS · {asof} ═══════════

FLEET
  {n_bots} bots · {tier_counts inline}
  Top 3 elite:   {bot, bot, bot}
  Top 3 dark:    {bot, bot, bot}

REGIME · {label} (confidence {0.XX})
  Recommended pack: {pack_name}
  {one-line rationale}

ACTIVE OVERRIDES
  {n_size_pins} size_modifiers · {n_school_pins} school_weights
  Notable: {bot_id @ X% for Y h} · ...
  (none) — if no overrides active

PERFORMANCE — LAST 7 DAYS
  Winners:  {bot} +{R}R · {bot} +{R}R · {bot} +{R}R
  Losers:   {bot} {R}R · {bot} {R}R · {bot} {R}R

KELLY RECOMMENDATIONS (top 5 by confidence)
  {bot}: recommend {X}× (currently {Y}×)  µ={R} σ={R}
  ... (5 max)

SENTIMENT
  BTC: fear/greed {fg}, vol_z {z}σ
  ETH: fear/greed {fg}, vol_z {z}σ
  (or "no recent snapshot" — fetch task may be off)

UPCOMING (≤60min)
  {N} events · severity ≥2: {list}
  (none in window) — if empty

INFRASTRUCTURE
  Dark modules: {n} ({names if any})
  Bots online (agent bus): {n} ({list})
  Topology: {n_nodes} nodes / {n_edges} edges

═══════════════════════════════════════════
```

## Discipline rules

* **One call per session for routine use.** Zeus caches for 30s — the
  operator who asks "zeus" twice in a row gets the cached view. To
  force a fresh build (e.g. just-applied an override), pass
  `force_refresh=true`.
* **Render in the order above.** Operator's eyes train to it; reordering
  costs them seconds.
* **Empty sections render as `(none)`.** Don't omit — the visual rhythm
  is part of the skill.
* **Cite numbers, not feelings.** "Top dark bot is rsi_mr_mnq -0.32"
  not "rsi_mr_mnq doesn't look great". The narrative layer goes on top
  of the data, not in place of it.

## Operator follow-ups (where to go next)

After Zeus, common operator next-steps are pre-mapped:

| Operator says... | Next skill to activate |
|---|---|
| "Why is X losing?" | `jarvis-anomaly-investigator` (T11 anomaly investigation) |
| "Trim X" | `jarvis-council` if X is ELITE/PRODUCER, else direct override |
| "Apply the regime pack" | use `jarvis_apply_regime_pack` after operator confirms |
| "What if I pinned X to 0.5?" | `jarvis_counterfactual` (T7) |
| "Daily review" | `jarvis-daily-review` (T4) |
| "Show me the chart" | `jarvis-topology` (T17) for force-directed view |

## Cost

ONE jarvis_zeus call ≈ 0.3-0.5s of in-process Python (no LLM, no
broker, no network outside the SSH tunnel to the cache files). The
LLM step happens when Hermes narrates the result — one chat completion
at ~$0.05.

So a full Zeus query costs ~$0.05 + 1-3s wall time. Hardware-cheap.

## Why this is "Zeus Supercharge"

Each of the 17 tracks gave the operator a lens. Zeus is the
**meta-lens** — a single view that composes them all without forcing
the operator to remember which skill answers which question.

After this, the operator's morning flow is:

```
06:30 UTC — morning_briefing cron fires → telegram ping
09:30 ET  — operator opens Hermes-desktop → says "zeus"
            → reads the unified view in 15s
            → if anything anomalous, runs the specific drill-down skill
15:00 ET  — daily_review cron fires → telegram ping
20:00 UTC Sun — weekly_review cron fires (if wired) → reads trade journal
```

The operator's cognitive load goes from "which lens do I need" to
"what should I act on" — Zeus shows the state; the operator decides.

## Edge cases

* **Cache stale on first call after restart** — first call rebuilds
  (~0.5s); subsequent calls within 30s are instant.
* **Sub-fetch failure** — that key has `{"error": "..."}` in the payload;
  Zeus renders that as `(unavailable: <reason>)` in its slot.
* **Empty fleet** (fresh install) — Zeus renders skeleton with
  `(no kaizen report yet — supervisor inactive or not yet run)`.
