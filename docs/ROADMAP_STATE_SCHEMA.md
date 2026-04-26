# `roadmap_state.json` schema

> Status: living document
> Owner: Apex Predator operator + Claude Code sessions
> Last verified against: `v0.1.71` snapshot (38 milestones, 103
> shared-artifact entries)

`roadmap_state.json` is the structured progress tree for the project.
Every `vX.Y.Z` milestone bumps it via
`scripts/_new_roadmap_bump.py` (or a one-shot `scripts/_bump_roadmap_vX_Y_Z.py`).
It's ~330KB on disk because the audit-trail decisions are kept inline
rather than in a separate database.

This doc exists so future sessions can read/edit safely without
re-deriving the shape every time.

## Top-level keys

| Key | Type | Purpose |
|---|---|---|
| `last_updated` | ISO-8601 string | Most recent edit, set by every bump script. |
| `last_updated_utc` | ISO-8601 string | Mirror of `last_updated` -- some readers historically used the suffixed name. Both are kept in sync. |
| `overall_progress_pct` | int | Coarse phase-completeness. Most bump scripts leave this at 100 since v0.1.x is post-scaffold. |
| `current_phase` | string | Free-text label of where the project is in the ROADMAP.md plan. |
| `mnq_engine_bridge` | dict | Live telemetry block for the MNQ engine (latest equity, open positions, etc.). Updated by the runtime, not by bump scripts. |
| `shared_artifacts` | dict | The big bucket. Every shipped bundle adds a key like `apex_predator_v0_1_71_pr2_spec_first_cluster`. Plus running counters (`apex_predator_tests_passing`, `databento_rows`, etc.). |
| `phases` | list | One dict per ROADMAP.md phase (P0 → P12). Each phase has `name`, `weeks`, `status`, `deliverable`. Updated when a phase flips status. |
| `bot_status` | dict | Per-bot live state (mnq, nq, crypto_seed, eth_perp, sol_perp, xrp_perp). Updated by the runtime. |
| `mcp_status` | dict | MCP / supervisor health flags. |
| `milestones` | list | Append-only ledger of every shipped `vX.Y.Z`. New entries go on the end via dedup-safe append (see "milestones" section). |

## `shared_artifacts.apex_predator_vX_Y_Z_<slug>` (per-bundle records)

Every bump script adds one of these. Schema converged around v0.1.50:

```jsonc
{
  "timestamp_utc": "2026-04-26T18:35:13.878490+00:00",
  "version":       "v0.1.71",
  "bundle_name":   "PR #2 SPEC-FIRST CLUSTER CLOSURE -- ...",
  "theme":         "free-text paragraph explaining the why",
  "modules_added":   ["apex_predator/core/kill_switch_latch.py", ...],
  "modules_edited":  ["apex_predator/strategies/engine_adapter.py (...)", ...],
  "tests_added_in_this_bundle":   132,
  "tests_added_lower_bound":      132,
  "tests_passing_before":         4403,
  "tests_passing_after":          4403,
  "ci_status_after_merge":        "all_green",
  "ci_jobs_green":  ["ruff (production code) [py3.14]", ...],
  "operator_directive_quote":     "continue do all",
  "design_choices":  { "<topic_snake_case>": "free-text rationale", ... },
  "scope_exclusions": { "<topic_snake_case>": "free-text deferral", ... },
  "ruff_green_touched_files":     true
}
```

Optional / contextual keys (not all bundles use all of them):

| Key | When |
|---|---|
| `r1_closure_state` etc. | Per Red Team residual closure ledgers. |
| `tests_delta_residual_from_other_modules` | When the bundle landed alongside out-of-scope test additions. |
| `full_suite_runtime_seconds` | Performance tracking. |
| `migration_notes` | When the bundle changes a public API. |

The keys are intentionally free-form -- this is a ledger, not a strict
contract. The dedup guard in
`scripts/_new_roadmap_bump.py` prevents double-applying the same key
on rerun.

## `milestones` (append-only)

```jsonc
[
  ...,
  {
    "version":       "v0.1.71",
    "timestamp_utc": "2026-04-26T18:35:13.878490+00:00",
    "title":         "<one-line summary>",
    "tests_delta":   132,
    "tests_passing": 4403
  }
]
```

Bump scripts should:

1. Append iff the version isn't already present (the v0.1.71 bump
   uses `if not any(m.get("version") == VERSION for m in milestones)`
   as the dedup guard).
2. Match the previous bundle's `tests_passing` numbers (or carry
   forward + record the source of the discrepancy in `theme`).

## `shared_artifacts` running counters

Top-level keys without the `apex_predator_v...` prefix are running
counters or singletons:

| Key | Value | Updated by |
|---|---|---|
| `apex_predator_tests_passing` | int | Every bump script |
| `databento_rows`, `databento_pull_usd` | int / float | Data ingest |
| `mnq_test_suite_passing` | int | MNQ-specific bump |
| `apex_go_state` | dict of `<bot>_live: bool` flags | `scripts.go_trigger` |
| `last_firm_verdict_summary` | dict | `scripts.engage_firm_board` |

Treat these as live state -- editing them from a bump script is rare
and should be paired with the matching code change.

## `phases`

```jsonc
[
  {
    "id":          "P0",
    "name":        "Scaffold & Blueprint Lock",
    "weeks":       1,
    "status":      "DONE",
    "deliverable": "ROADMAP.md + config.json + ..."
  },
  ...
]
```

Status values seen in the wild: `DONE`, `IN_PROGRESS`, `OPEN`, `BLOCKED`,
`SCAFFOLDED`. There's no enum enforcement -- the operator + bump scripts
keep this honest.

## How to write a new bump script

Lift the most recent `scripts/_bump_roadmap_v0_1_*.py` as a template,
then change:

1. `VERSION` constant.
2. `PRIOR_TESTS_ABS` (or `NEW_TESTS_ABS`) -- pull from the actual
   pytest output of the bundle.
3. The `key` that goes into `shared_artifacts` -- pattern is
   `apex_predator_v0_<major>_<minor>_<slug>` where slug is
   snake_case and unique.
4. The `bundle_name`, `theme`, `modules_added/edited`,
   `design_choices`, `scope_exclusions` blocks. Tone: explain the
   non-obvious bits and the reasoning, not just the file diff.

Run the script with `PYTHONPATH=/tmp/_pkg_root:.` set so
`from apex_predator.X` imports resolve, then verify with:

```bash
python3 -c "
import json
d=json.load(open('roadmap_state.json'))
last=d['milestones'][-1]
print(last['version'], last['tests_passing'])
"
```

The dedup guard means re-running the same script is safe -- the second
invocation is a no-op.

## Editing the file directly

Don't, except in narrow cases:

* **Live-telemetry corruption** -- if the runtime wrote a malformed
  `mnq_engine_bridge` block, hand-edit + commit with a
  `chore(roadmap): repair <field>` message.
* **Schema migration** -- if the schema changes, write a one-shot
  `scripts/_migrate_roadmap_<from>_to_<to>.py` that's idempotent
  and ship it as a milestone.

Otherwise, prefer adding a bump script. The audit trail is the
point.
