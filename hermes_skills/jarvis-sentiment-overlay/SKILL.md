---
name: jarvis-sentiment-overlay
version: 1.0.0
description: Pulls LunarCrush sentiment (crypto) and macro news flow on a schedule, writes a JARVIS-readable feature, surfaces fear/greed regime hints.
tags: [trading, sentiment, lunarcrush, macro, overlay]
trigger_phrases:
  - "sentiment overlay"
  - "sentiment check"
  - "what's the social mood"
  - "fear greed"
  - "crypto twitter"
---

# jarvis-sentiment-overlay (T16)

Hermes-driven sentiment fetcher that turns external chatter into a
JARVIS-consumable feature. Runs on a 15-minute cadence (or on-demand
when the operator asks) and populates the sentiment cache that
`portfolio_brain.assess` and `hermes_overrides` skills can consult.

The actual SIZING decision is still made by portfolio_brain and the
operator — this skill just provides one more input feature.

## Two modes

### Mode A: scheduled fetch (background)

Wired as a scheduled_task in `~/.hermes/config.yaml`:

```yaml
- name: sentiment_fetch
  cron: "*/15 * * * *"   # every 15 min
  delivery: webhook       # suppress chat delivery; this is data plumbing
  delivery_extra:
    suppress_if_unchanged: true
  prompt: |
    Activate the jarvis-sentiment-overlay skill in FETCH mode for these
    assets: BTC, ETH, SOL, macro. For each, call the LunarCrush MCP
    tools to pull current sentiment, distill into a fear_greed scalar
    [0.0, 1.0], social_volume_z, and topic_flags, then call
    sentiment_overlay.write_sentiment_snapshot(asset_class, snapshot)
    via a python tool exec. Reply quietly with "fetched N assets".
```

### Mode B: operator query (foreground)

Trigger phrases above. Operator asks "what's the social mood on BTC"
and the skill:

1. Calls LunarCrush MCP for current BTC creator-time-series + posts.
2. Pulls the cached snapshot (if recent) via
   `sentiment_overlay.current_sentiment("BTC")`.
3. Renders the operator-readable brief.

## Fetch flow (Mode A)

For each asset (crypto path):

1. `mcp__4e13b96c-...__Cryptocurrencies` → grab the symbol's current
   metrics, social volume, sentiment score.
2. `mcp__4e13b96c-...__Topic_Time_Series(topic=<asset_name>, period=24h)`
   → grab the 24h sentiment trend.
3. Synthesize into:

```python
snapshot = {
    "fear_greed": fg_scalar,     # 0..1 (LunarCrush galaxy_score or
                                  # custom-derived from posts)
    "social_volume_z": z,         # z-score of social_volume vs 30d
    "topic_flags": {
        "squeeze": <bool: is "short squeeze" in top 10 topics?>,
        "capitulation": <bool: "capitulation" / "rekt" trending?>,
        "fomo": <bool: "moon" / "to the moon" trending?>,
    },
    "raw_source": "lunarcrush",
    "extras": { ... },
}
```

For `macro` asset:

1. Macro RSS or Bigdata aggregator MCP (`mcp__2bbf6e92-...__bigdata_search`
   with relevant queries).
2. Pull "fear index" proxies — VIX-related coverage, "recession"
   mentions, etc.
3. Synthesize same shape as crypto but topic_flags include `"fomc"`,
   `"jobs"`, `"earnings_blowup"`.

Then call (within the skill's Python tool surface):

```python
from eta_engine.brain.jarvis_v3 import sentiment_overlay
sentiment_overlay.write_sentiment_snapshot(asset_class, snapshot)
```

## Query flow (Mode B)

Operator: "sentiment check on BTC"

```
═══ BTC SENTIMENT · {asof} ═══

  fear/greed: {0.XX}  (0=peak fear, 1=peak greed)
  social volume z: {±X.X}σ vs 30-day
  active topics: {top 3 topic flags}

  Last 24h trend: {↑ rising | → flat | ↓ falling}

  What this hints for sizing:
  • {if fg ≥ 0.85 + high social volume}: trim momentum overlays;
    euphoria tends to mean-revert quickly
  • {if fg ≤ 0.15 + capitulation flag}: opportunity, but watch for
    further dump; mean_revert school usually outperforms here
  • {else}: no actionable hint; sentiment is in normal range

  Operator next step (optional):
    • Pin school weight: jarvis_pin_school_weight asset=BTC...
    • Trim a specific bot: jarvis_set_size_modifier bot_id=...
```

## Integration with portfolio_brain

Once the cache is populated, `portfolio_brain.assess` can read
`sentiment_overlay.current_sentiment(asset_class)` as one of its
inputs. **This integration is opt-in**: portfolio_brain doesn't read
sentiment by default (we'd need a careful A/B before changing the
sizing cascade). When the operator turns it on, a new rule is added to
the cascade:

* `if fear_greed > 0.85 AND social_volume_z > 1.5`: multiply size by
  0.7 (euphoria detected, trim by 30%)
* `if fear_greed < 0.15 AND capitulation flag`: no sizing change,
  but FLAG to operator (capitulation can mean opportunity or further
  dump — let operator decide)

Operator enables via Hermes-overrides:
```
operator: "from now on, use sentiment as a sizing factor for crypto bots"
hermes:   activates jarvis-sentiment-overlay; writes a "sentiment_enabled"
          fact to memory; portfolio_brain reads memory before each
          consult and dispatches.
```

(Full integration into portfolio_brain is a follow-up task. This skill
+ module sets up the data plumbing so the integration is a small,
isolated change later.)

## Cost

* Sentiment fetch every 15 min: ~96 polls/day. Each poll = 1-2 chat
  completions + 2-3 MCP calls. Estimated: $1-3/day = $30-90/month
  during research, dropping to $10-30/month after the operator
  finalizes their assets-of-interest list.

* On-demand operator queries: ~$0.10 each. Negligible.

## Memory save

After each fetch, if the snapshot differs meaningfully from the prior
one (fear_greed delta > 0.15 OR a new topic flag appears), save:

> subject="sentiment:{asset}"
> predicate="snapshot at {asof}"
> object="fear_greed={fg}, top_flag={flag}"
> trust_score=0.3  (sentiment is noisy; trust score stays low)

Compounds with T8 regime classifier training data.

## Edge cases

* **LunarCrush rate-limited**: skip this poll; next 15-min tick retries.
  The sentiment cache returns the most recent successful poll as long
  as it's < 60 min old.
* **Macro aggregator down**: same — skip silently.
* **Asset has no sentiment cache yet**: `current_sentiment(asset)`
  returns None; portfolio_brain treats it as "no feature available"
  and proceeds with the rule cascade unchanged.

## Why deterministic write, LLM read

The FETCH path produces deterministic numbers (fear_greed scalar,
volume z-score). The READ path is where Hermes shines — interpreting
the numbers in context, surfacing topic_flags as narrative warnings.
This split keeps the data layer cheap and the narrative layer
intelligent.
