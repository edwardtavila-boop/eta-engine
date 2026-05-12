# T6 Causal Layer + T7 Replay Engine — Architecture Spec

Detailed implementation plan for the two analytical-foundation tracks
that unlock most of the T8-T13 follow-on tracks. T6 and T7 share
foundational data (trace schema v2 — see `TRACE_SCHEMA_V2_DESIGN.md`)
so we plan them together.

**Status:** specification only. Implementation queued to start when the
operator triggers "begin T6". Estimated: ~3-4 weeks of focused work for
both at supercharged quality (full tests, polish, docs, deployment).

## Goals

* **T6 — Causal layer:** Given a consult, tell the operator *which
  schools' votes mattered most*. Not just "the verdict was PROCEED at
  0.7×" but "mean_revert flipping its vote would have changed the
  verdict; momentum flipping wouldn't have." Marginal-effect attribution.

* **T7 — Replay engine:** Given a consult that happened, let the
  operator ask "what would have happened if X was different?" — replay
  with hypothetical overrides, alternate inputs, or alternate school
  configurations. Deterministic re-execution of the full cascade.

The common thread: both require enough state in the trace to
reconstruct the consult fully. v1 trace records aren't enough — see
schema v2 spec.

## Design

### T6 module layout

```
eta_engine/brain/jarvis_v3/
├── causal_attribution.py     [NEW]
└── (no changes to existing modules)
```

**`causal_attribution.py` public interface:**

```python
def analyze(consult_id: str, *, perturbation_sigma: float = 1.0) -> CausalReport
    """Run marginal-effect attribution on the consult identified by
    consult_id. Returns a CausalReport with per-school marginal effects
    and a "decisive schools" set. NEVER raises — returns an error
    envelope if the consult isn't found or is pre-v2."""

@dataclass
class CausalReport:
    consult_id: str
    base_verdict: dict           # what actually happened
    per_school_attribution: list[SchoolAttribution]
    decisive_schools: list[str]  # schools whose flip changes the verdict
    perturbation_sigma: float

@dataclass
class SchoolAttribution:
    school: str
    base_score: float
    flipped_score: float
    base_verdict: str
    flipped_verdict: str
    marginal_size_delta: float
    is_decisive: bool
```

**Algorithm:**

1. Load trace record for consult_id. If not v2, return error.
2. Reconstruct the consult inputs from the v2 record:
   * school_inputs → per-school score & rationale
   * portfolio_inputs → PortfolioContext
   * hot_weights_snapshot → hot-learner overlay
   * overrides_snapshot → operator pins (if any were live)
3. **Base run:** re-execute the consolidator with the original inputs.
   Confirm the resulting verdict matches the trace's recorded verdict
   within tolerance (sanity check — diverging = bug or v1 record).
4. **Perturbation runs (one per school):**
   * For each school in school_inputs:
     * Copy the inputs dict.
     * Replace this school's score with `base_score - perturbation_sigma`
       (the "what if this school said NO instead?" counterfactual).
     * Re-run the consolidator with the perturbed inputs.
     * Record the resulting verdict + size delta vs base.
5. Identify decisive schools (verdict text differs from base) and
   compose the report.

**Tool wiring:** new MCP tool `jarvis_explain_consult_causal` in
`jarvis_mcp_server.py`. Handler reads the report via
`causal_attribution.analyze` and returns the structured CausalReport
dict.

**Tests:**
* `test_attribution_runs_on_v2_record`: full happy path.
* `test_attribution_rejects_v1_record`: returns error envelope.
* `test_attribution_handles_missing_consult`: error envelope, no raise.
* `test_decisive_school_detection_for_50_50_split`: synthetic 2-school
  consult where one school is 0.51 and the other is 0.49; flipping the
  0.51 school should produce a different verdict.
* `test_no_decisive_schools_when_consensus_is_strong`: synthetic 5-school
  consult where 4 schools vote 0.9 and 1 votes 0.1; no perturbation
  flips the verdict (consensus is robust).

### T7 module layout

```
eta_engine/brain/jarvis_v3/
├── replay_engine.py     [NEW]
└── (no changes to existing modules)
```

**`replay_engine.py` public interface:**

```python
def replay(
    consult_id: str,
    *,
    override_overrides: dict | None = None,
    override_hot_weights: dict | None = None,
    override_school_inputs: dict | None = None,
) -> ReplayResult
    """Re-execute the consult identified by consult_id, optionally with
    hypothetical overrides in place of the historical ones. Returns
    ReplayResult with the alt verdict, side-by-side diff vs base, and
    a determinism check.

    Three layer of overrides supported:
      * override_overrides: replaces hermes_overrides snapshot
      * override_hot_weights: replaces hot_learner snapshot
      * override_school_inputs: replaces (per-school) raw inputs
    """

@dataclass
class ReplayResult:
    consult_id: str
    base_verdict: dict   # from the trace
    replay_verdict: dict  # re-executed
    diff: dict           # field-by-field delta
    matched_base: bool   # True iff no overrides AND verdict reproduces
    overrides_applied: dict
```

**Algorithm:**

1. Load trace record. If not v2, return error.
2. Build replay inputs:
   * Start from the v2 record's school_inputs, portfolio_inputs,
     hot_weights_snapshot, overrides_snapshot.
   * For each `override_*` arg passed in, replace the corresponding
     piece.
3. Re-seed RNG from `rng_master_seed` (deterministic mode).
4. Walk the cascade:
   * Call `portfolio_brain.assess(req, ctx)` with the replay context.
   * Apply hot-weight overlay.
   * Apply Hermes-override overlay.
   * Run the consolidator on per-school inputs.
   * Produce the verdict.
5. Diff against the v2 record's stored verdict. Mark `matched_base`
   iff no overrides were passed AND the alt verdict exactly matches
   the historical.

**Why determinism check matters:** if replay produces a DIFFERENT
verdict than the original consult with the same inputs, our trace
schema is missing something OR the consult cascade has hidden state.
Either is a bug we need to fix. Running base-equality assertion on
every replay catches this fast.

**Tool wiring:** new MCP tool `jarvis_replay_at` in `jarvis_mcp_server.py`.
The tool accepts:

```yaml
jarvis_replay_at:
  args:
    consult_id: str
    override_overrides: object (optional)
    override_hot_weights: object (optional)
    override_school_inputs: object (optional)
```

Plus a sibling `jarvis_counterfactual` convenience tool:

```yaml
jarvis_counterfactual:
  args:
    consult_id: str
    pin_size_modifier: float (optional, [0.0, 1.0])
    pin_school_weight:
      school: str
      weight: float
  description:
    "Replay consult_id but pretend the operator had pinned size_modifier
     to N AND/OR school weight to W at consult time. Wraps jarvis_replay_at
     with override_overrides constructed from the convenience args."
```

**Tests:**
* `test_replay_with_no_overrides_reproduces_base_verdict`: bedrock
  determinism check — replay an unmodified consult, verdict must match.
* `test_replay_with_size_modifier_override`: pin 0.5× and confirm the
  alt verdict's final_size = 0.5 × base.
* `test_replay_with_school_weight_override`: boost momentum school to
  1.5× and confirm the alt verdict differs in the expected direction.
* `test_replay_with_rng_seed_reproducible`: a stochastic school produces
  the same vote on a second replay with the same seed.
* `test_replay_rejects_pre_v2_records`.
* `test_counterfactual_convenience_wraps_replay`: confirm the
  convenience tool routes through replay correctly.

### Shared infrastructure (lands before either T6 or T7 ships)

* **Schema v2 dataclass changes** in `brain/jarvis_v3/trace_emitter.py`
  (per `TRACE_SCHEMA_V2_DESIGN.md`).
* **Population of v2 fields at consult emission sites:**
  `brain/jarvis_v3/jarvis_conductor.py`,
  `brain/jarvis_v3/jarvis_full.py`. Each emission point captures the
  relevant snapshot before delegating to `trace_emitter.emit`.
* **`TraceRecord` migration helpers** in trace_emitter:
  * `is_v2_record(rec) -> bool` — quick check
  * `extract_replay_inputs(rec) -> ReplayInputs` — packed convenience
    type so both T6 and T7 don't reach into the dict manually.

### MCP tool surface (after T6 + T7 land)

Current: 16 tools (post-Track 2 + 1).
After T6/T7: **18 tools** (+jarvis_explain_consult_causal, +jarvis_replay_at,
+jarvis_counterfactual = 19 actually).

The mass operator-facing addition is `jarvis_counterfactual` —
operators don't usually call low-level replay; they want "what if I had
trimmed atr_breakout at 9am today?"

## Cross-track impact

T6/T7 unlock or substantially simplify:

* **T8 regime classifier**: training features now include per-school
  inputs and rng-determined stochastic outputs.
* **T11 adversarial inspector**: can run causal attribution to find
  the strongest perturbation that would have flipped the verdict, then
  argue it.
* **T12 attribution cube**: per-school inputs become a primary slice
  dimension.
* **T13 Kelly sizing optimizer**: can replay-test new sizing rules
  against the last 30 days of consults before applying live.
* **Daily review + drawdown response skills (already shipped)**: can
  add "I would have caught this with override X" hindsight to their
  output once T7 is live.

## Migration concerns

The trace stream during the migration window will contain a mix of
v1 and v2 records. Strategy:

1. **Day 0**: v2 schema ships. Emit sites still emit v1-style records
   (no v2 fields populated). No reader changes needed.
2. **Day 0-N**: progressively flip each emit site to populate v2 fields.
   Records emitted after each site update have the new fields.
3. **Day N**: all emit sites updated. Every NEW record is v2.
4. **Day N + 1**: T6 and T7 tools are released. They reject v1 records
   gracefully ("pre-v2 consult, no causal/replay data") and operate
   normally on v2 ones.

The mixed-mode period lasts however long step 2 takes. Operator-facing
impact during the window: zero (T6/T7 tools simply unavailable for old
consults).

## Resource estimates (supercharged quality)

| Phase | Work | Days |
|---|---|---|
| 1 | Schema v2 dataclass + tests | 1 |
| 2 | Populate emit sites (3-5 sites) | 2 |
| 3 | `causal_attribution.py` + 8 tests | 3 |
| 4 | `replay_engine.py` + 8 tests | 4 |
| 5 | MCP tool wiring + 3 tools + tests | 1 |
| 6 | Skills updates (daily-review, drawdown-response can now use replay) | 1 |
| 7 | Operator-facing docs | 1 |
| 8 | VPS deploy + e2e verification | 1 |

**Total: ~14 person-days for both tracks at supercharged quality.**
Could be parallelized to ~10 elapsed days with two agents working
concurrently after Phase 2 completes.

## Risks

* **Replay non-determinism**: if a school has non-captured hidden state
  (cache, global var, network call), replay won't match base. Mitigation:
  the base-equality assertion runs on every test consult during
  development; we fix any divergence before T7 ships.
* **Performance regression at emit time**: v2 captures ~1.5KB more per
  consult, but emission is already off the hot path. Expected impact:
  invisible.
* **Storage growth**: 2.5× trace file size. Mitigation: rotation
  already in place, gzip compression on rotation, no operator action
  needed.
* **Backward compatibility**: v1 records remain readable but lose
  access to T6/T7 features. Acceptable — that's not regression, it's
  upgrade.

## What's next after T6/T7

In the recommended cadence (from `HERMES_BRAIN_FUTURE_TRACKS.md`):

* **T12 attribution cube** can start in parallel with T7 once schema v2
  lands (both depend on the same school-inputs field).
* **T8 regime classifier** waits for T7 to backtest pack performance.
* **T11 adversarial inspector** can land anytime after T6 (it composes
  causal attribution).
* **T9 council** waits for T6/T7 because the council should have causal
  reasoning available when arguing about high-stakes decisions.

## Status

* This spec: shipped 2026-05-12 in the pre-T6 hardening sprint.
* Schema v2 design: shipped in `TRACE_SCHEMA_V2_DESIGN.md`.
* Implementation: queued to start when operator triggers T6
  (post-live-capital-cutover).
