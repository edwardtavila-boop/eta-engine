# Trace Schema v2 — Design Doc (T6/T7 Prerequisite)

Design for bumping `brain.jarvis_v3.trace_emitter.TraceRecord` to a v2
schema that captures enough state to support **causal-layer exposure
(T6)** and **deterministic replay (T7)**. Both depend on being able to
reconstruct a consult from its trace record alone — which v1 doesn't
quite support (lossy on RNG seeds, intermediate scores, ctx snapshot).

## Why v1 isn't enough

v1's `TraceRecord` captures:

```
ts, bot_id, consult_id, action, verdict (final dict),
schools (final dict), clashes, dissent, portfolio,
context (final dict), hot_learn, final_size, block_reason,
elapsed_ms, hermes_calls
```

Missing for replay / causal attribution:

1. **Per-school intermediate scores** before the consolidator merges
   them. v1 stores the consolidated `schools` dict but loses each
   school's raw input — so we can't perturb one school's vote and
   re-cascade.
2. **RNG seeds** that any school used (bootstrap intervals, monte-carlo
   sampling, anything with a random component). Without these, replay
   isn't deterministic even with the same inputs.
3. **Portfolio context inputs** at consult time. v1 stores `portfolio`
   as the final block result, not the inputs that were fed to
   `portfolio_brain.assess`. We need the input snapshot to replay.
4. **Hot-learner weights snapshot** — what `current_weights(asset)`
   returned at consult time. Hot-learner state evolves, so by the time
   we replay a week-old consult, the weights have drifted. Need the
   point-in-time snapshot.
5. **Hermes-overrides snapshot** — what overrides were active at
   consult time (T2 added this; v1 captures `hermes_calls` but not
   the active-pin snapshot).
6. **Trace schema version field** — so readers can dispatch between
   v1 and v2 records mixed in the same file during the migration window.

## v2 schema (additions only — v1 fields preserved)

```python
@dataclass
class TraceRecord:
    # ─── v1 fields (unchanged) ──────────────────────────────────────
    ts: str = ""
    bot_id: str = ""
    consult_id: str = ""
    action: str = ""
    verdict: dict = field(default_factory=dict)
    schools: dict = field(default_factory=dict)
    clashes: list = field(default_factory=list)
    dissent: list = field(default_factory=list)
    portfolio: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    hot_learn: dict = field(default_factory=dict)
    final_size: float = 0.0
    block_reason: str | None = None
    elapsed_ms: float = 0.0
    hermes_calls: dict = field(default_factory=dict)

    # ─── v2 additions ───────────────────────────────────────────────
    schema_version: int = 2
    school_inputs: dict = field(default_factory=dict)
    """Per-school RAW input snapshot keyed by school name. Each value
    is the school's pre-consolidation vote dict containing at least:
        {"score": float, "size_modifier": float, "rationale": str,
         "rng_seed": int | None}
    rng_seed is None for deterministic schools, int for any school
    that drew from a random generator during this consult.
    Used by: T6 (perturb one school, observe verdict shift),
             T7 (deterministic replay)."""

    portfolio_inputs: dict = field(default_factory=dict)
    """Snapshot of the PortfolioContext fed to portfolio_brain.assess.
    Includes fleet_long_notional_by_asset, fleet_short_notional_by_asset,
    recent_entries_by_asset, open_correlated_exposure,
    portfolio_drawdown_today_r, fleet_kill_active. Replay-critical."""

    hot_weights_snapshot: dict = field(default_factory=dict)
    """{school: weight} for this bot's asset_class at consult time.
    Snapshot of hot_learner.current_weights() result. Required for
    replay because hot_learner state drifts after the consult."""

    overrides_snapshot: dict = field(default_factory=dict)
    """{size_modifier: float | None, school_weights: {school: weight}}
    for this bot/asset at consult time. Captures Hermes-pinned
    overrides that were live. Required for replay because overrides
    expire (TTL-bounded)."""

    rng_master_seed: int | None = None
    """The deterministic master seed for this consult, if the
    supervisor used one. Replay re-seeds from this value before
    invoking school evaluators."""
```

## Migration plan

### Phase 1 — Add fields (backward compatible)

* Add the v2 fields to `TraceRecord` with safe defaults (empty dicts /
  None). v1 readers that don't know about them simply ignore the new
  keys in the JSONL row.
* Add `schema_version: 2` field; v1 records implicitly = 1.
* Ship the dataclass change in a feat commit. NO consumer code changes
  yet.

### Phase 2 — Populate (per call site)

* Update each emitter call site in `jarvis_conductor.py` /
  `jarvis_full.py` to populate the new fields. Each site:
  * Saves the school inputs dict BEFORE consolidation.
  * Captures `ctx.as_dict()` at the start of `portfolio_brain.assess`.
  * Grabs `hot_learner.current_weights(asset_class)` at consult start.
  * Pulls `hermes_overrides.get_size_modifier(bot_id) +
    get_school_weights(asset_class)` at consult start.
  * Records the master seed used by the RNG (or None).

* Each site that adds a field is a separate commit so we can roll back
  granularly if any site introduces a perf regression.

### Phase 3 — Reader updates

* `trace_emitter.tail()` and `read_since()` already return raw dicts
  with `dict.get()` semantics — they'll surface the new fields if
  present, ignore them if absent. **No reader changes needed.**

* `jarvis_explain_verdict` MCP tool stays the same — it operates on
  whichever fields exist. v2 records yield richer narratives.

* Tools that consume the new fields (T6's `jarvis_explain_consult_causal`,
  T7's `jarvis_replay_at`) are written AGAINST v2 directly. They
  return a "v1_record_no_replay_data" error envelope when given a v1
  record.

### Phase 4 — Backfill (optional, can skip)

For records emitted before v2 lands, we can't reconstruct the missing
fields. T6/T7 simply won't work on pre-v2 consults. That's acceptable —
the operator's interesting consults are recent ones (today, this week,
this month), and v2 ships before those windows.

If we ever want to backfill: keep a per-bot replay-able-state journal
that captures the missing fields independently. NOT planned — adds
complexity, modest payoff.

## Performance impact

Estimated v2 record size: **+1.5 KB** average over v1 (~1.0 KB).
Effective payload bloat: 2.5× per record.

At a steady fleet rate of ~500 consults/day:

* v1 daily trace volume: ~0.5 MB/day → 15 MB/month
* v2 daily trace volume: ~1.25 MB/day → 38 MB/month

Rotation threshold stays at 10 MB → v2 rotates every ~8 days vs v1's
every ~20 days. Gzipped rotation files compress ~10×, so disk pressure
is trivial.

Latency: v2 emission adds maybe 50–100µs per consult (dict
construction + json serialization of extra fields). Trace emission is
already off the trade-execution hot path — it runs after the verdict
is delivered — so this is invisible to fill latency.

## Validation tests required

* `test_v2_emit_then_read_roundtrip`: emit a v2 record with all new
  fields populated, read it back via `tail()`, verify every field
  survives JSON serialization.
* `test_v1_record_readable_with_v2_dataclass`: load a synthetic v1
  JSONL line and confirm the v2 dataclass parses it with empty defaults
  for the missing fields.
* `test_v2_record_has_replayable_fields`: assert that a v2 record
  contains enough information to reconstruct a `PortfolioContext`,
  `hot_learner.current_weights` output, and per-school inputs.
* `test_rng_seed_captured_for_stochastic_schools`: emit a consult that
  uses the Bayes-bootstrap school (stochastic), verify rng_seed in
  school_inputs is non-None.

## What this UNLOCKS

* **T6 causal layer**: `jarvis_explain_consult_causal(consult_id)`
  reads the v2 record, perturbs each school's `score` by ±1σ, re-runs
  the consolidator, returns the marginal-effect attribution table.
* **T7 replay engine**: `jarvis_replay_at(consult_id,
  override_overrides={})` reconstructs the consult inputs from the
  v2 record, re-runs the full cascade with the operator's hypothetical
  overrides in place of the historical ones, returns the alt verdict.
* **T8 regime classifier**: trains on per-school-input features
  alongside the consult outcomes — only possible with v2's
  school_inputs.
* **T12 attribution cube**: can slice trades by (school × asset ×
  regime × hour-of-day) because school-level inputs are now captured.

## Open questions

* Should `rng_master_seed` live on the TraceRecord OR on a sibling
  `consult_metadata` stream? Argument for sibling: keeps the trace
  stream replay-input-only, separates "what happened" from "what the
  cascade was given". Argument for inline: simpler, one file per
  consult is the operator's mental model. **Default: inline on the
  TraceRecord. Revisit if it turns out we want consult_metadata for
  other reasons (T14 multi-agent attribution).**

* Snapshot vs reference: when capturing `overrides_snapshot`, do we
  store the values or a pointer to the override entry by its applied_at
  ISO? Argument for values: replay works even if the override was
  cleared by hand later. Argument for reference: smaller record.
  **Default: store values inline. Override entries are tiny.**

* Schema version negotiation in `read_since()`: should the tool surface
  expose `min_schema_version` filter so T6/T7-aware clients can
  fast-skip v1 records? **Default: no, post-process on the client.
  Keeps the MCP surface stable.**

## Estimated effort

* Schema additions: ~50 LOC + tests (~1 day)
* Call site population (3–5 sites): ~200 LOC + tests (~2 days)
* Validation test battery: ~150 LOC (~0.5 day)
* Docs update: ~0.5 day

**Total: ~4 person-days for the prerequisite schema bump.** Counted
inside T6's implementation budget (Track 6 in the future-tracks doc).

## Status

* This doc: shipping in the pre-T6 hardening sprint (2026-05-12).
* Implementation: queued to start when operator triggers T6
  (post-live-capital-cutover).
