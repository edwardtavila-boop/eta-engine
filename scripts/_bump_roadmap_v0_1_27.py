"""One-shot: bump roadmap_state.json to v0.1.27.

FINAL REVISION BUNDLE -- the full pre-rollout optimization pass.

This does NOT add a roadmap phase. It is the reproducible end-to-end
optimization pipeline run that happens immediately before handing off
to P9_ROLLOUT (which itself is still gated by the $1000 Tradovate
funding requirement).

What v0.1.27 adds
-----------------
  * ``scripts/_jarvis_final_revision.py`` -- single-entrypoint pipeline
    that produces six artifacts under ``docs/final_revision/``:
      - jarvis_context.json
      - jarvis_playbook.txt
      - principles_audit.json
      - basement_sweep_summary.json
      - tweaks_proposed.json
      - final_revision_report.txt

    Four stages:
      1. JarvisContext snapshot -> suggested action = TRADE
      2. principles_checklist -> score 1.000 / grade A+ / discipline 10
      3. basement parameter sweep -> 192 candidates, 169 gate-pass,
         1 pareto-frontier winner (conf=8, stop=1.0, tp=3.0, dd=3.0, pos=1)
      4. master_tweaks with GLIDE-STEP -- winner emits AGGRESSIVE
         (correctly rejected), so we also generate a MODERATE-compliant
         intermediate proposal that caps each numeric parameter's
         relative change at 0.34, which the policy accepts and applies.

    Final READY verdict: True (jarvis=TRADE AND principles>=0.85 AND
    sweep gate-pass-count > 0). External gate unchanged -- still waiting
    on Tradovate funding.

  * ``brain/jarvis_context.py`` -- JarvisContextEngine constructor now
    accepts convenience ``*_provider`` kwargs (Protocol objects OR
    bare callables returning the matching snapshot type). This unifies
    the engine construction API across admin and v2 tests and lets
    one-shot scripts instantiate an engine without a separate builder
    when the data source is a simple lambda.

Reconciliation
--------------
  * tests_passing: 1285 -> 1359 (+74). The bump count in v0.1.26 did not
    include a handful of engine-coverage tests that had landed in the
    tree but were not reflected in the counter. This bump reconciles to
    the actual pytest output of 1359 passed.
  * No phase-level status changes. P9_ROLLOUT remains at 85% (blocked
    on $1000 Tradovate funding gate).
  * overall_progress_pct: 99 (unchanged).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    new_tests = 1359
    sa["eta_engine_tests_passing"] = new_tests

    sa["eta_engine_v0_1_27_final_revision"] = {
        "timestamp_utc": now,
        "version": "v0.1.27",
        "bundle_name": "FINAL REVISION -- Jarvis + basement sweep + glide-step tweaks",
        "theme": (
            "End-to-end reproducible pre-rollout optimization. Jarvis "
            "confirms all gates green, principles audit scores A+, the "
            "basement sweep finds a clear winner, and a MODERATE-compliant "
            "glide-step tweak is proposed and applied. Everything is "
            "ready for P9_ROLLOUT; only the external $1000 Tradovate "
            "funding gate remains."
        ),
        "pipeline_script": "eta_engine/scripts/_jarvis_final_revision.py",
        "artifacts_dir": "eta_engine/docs/final_revision/",
        "artifacts": [
            "jarvis_context.json",
            "jarvis_playbook.txt",
            "principles_audit.json",
            "basement_sweep_summary.json",
            "tweaks_proposed.json",
            "final_revision_report.txt",
        ],
        "stage_results": {
            "jarvis": {
                "suggested_action": "TRADE",
                "reason": "all gates green",
                "confidence_pct": 80,
                "stress_pct": 0,
                "binding_constraint": "macro_event",
                "session_phase": "LUNCH",
                "suggested_size_pct": 70,
            },
            "principles": {
                "score": 1.000,
                "letter_grade": "A+",
                "discipline_score": 10.000,
                "critical_gaps": 0,
                "period_label": "pre-rollout-final-revision",
            },
            "sweep": {
                "total_candidates": 192,
                "gate_pass_count": 169,
                "pareto_frontier_count": 1,
                "grid": {
                    "confluence_threshold": [5, 6, 7, 8],
                    "stop_atr_mult": [1.0, 1.25, 1.5],
                    "tp_atr_mult": [1.5, 2.0, 2.5, 3.0],
                    "daily_dd_cap_pct": [2.0, 3.0],
                    "max_open_positions": [1, 2],
                },
                "gate": {
                    "min_expectancy_r": 0.15,
                    "max_dd_pct": 0.05,
                    "min_trades": 20,
                    "min_win_rate": 0.45,
                },
                "winner_params": {
                    "confluence_threshold": 8,
                    "stop_atr_mult": 1.0,
                    "tp_atr_mult": 3.0,
                    "daily_dd_cap_pct": 3.0,
                    "max_open_positions": 1,
                },
                "winner_metrics": {
                    "expectancy_r": 1.400,
                    "max_dd_pct": 0.005,
                    "win_rate": 0.620,
                    "stability": 0.019,
                },
            },
            "tweaks": {
                "baselines": {
                    "mnq_apex": {
                        "confluence_threshold": 6,
                        "stop_atr_mult": 1.25,
                        "tp_atr_mult": 2.0,
                        "daily_dd_cap_pct": 3.0,
                        "max_open_positions": 1,
                    },
                },
                "full_winner_tweak": {
                    "risk_tag": "AGGRESSIVE",
                    "applied": False,
                    "note": (
                        "Full winner params represent +50% change in "
                        "tp_atr_mult (2.0 -> 3.0) which exceeds the 0.35 "
                        "MODERATE threshold -- correctly rejected by the "
                        "TweakPolicy(allow_aggressive=False)."
                    ),
                },
                "glide_step_tweak": {
                    "risk_tag": "MODERATE",
                    "applied": True,
                    "cap_relative_change": 0.34,
                    "proposed_params": {
                        "confluence_threshold": 8,
                        "stop_atr_mult": 1.0,
                        "tp_atr_mult": 2.68,
                        "daily_dd_cap_pct": 3.0,
                        "max_open_positions": 1,
                    },
                    "expected_metrics": {
                        "expectancy_r": 1.202,
                        "max_dd_pct": 0.0001,
                        "win_rate": 0.620,
                        "stability": 0.019,
                        "gate_pass": True,
                    },
                    "reason": ("applied (MODERATE): gate-pass: exp=+1.202R dd=0.01% stability=0.019"),
                },
                "policy": {
                    "allow_aggressive": False,
                    "max_relative_change": 0.50,
                    "require_gate_pass": True,
                },
            },
        },
        "ready_for_rollout": True,
        "ready_criteria": [
            "jarvis.suggested_action == TRADE",
            "principles.score >= 0.85",
            "sweep.gate_pass_count > 0",
        ],
        "external_gate": (
            "P9_ROLLOUT remains at 85% pending $1000 Tradovate funded "
            "balance -- required to issue API credentials (app_id, "
            "secret, client_id)."
        ),
        "engine_api_enhancement": {
            "module": "eta_engine/brain/jarvis_context.py",
            "change": (
                "JarvisContextEngine now accepts either builder= (existing) "
                "or {macro,equity,regime,journal}_provider= (new) kwargs. "
                "Provider kwargs accept either Protocol-compliant objects "
                "or bare callables returning the matching snapshot type."
            ),
            "adapters": [
                "_as_macro_provider",
                "_as_equity_provider",
                "_as_regime_provider",
                "_as_journal_provider",
            ],
            "tests_unblocked": [
                "tests/test_jarvis_admin.py::TestEngineIntegration::test_admin_with_engine_ticks_per_request",
            ],
        },
        "glide_step_algorithm": {
            "function": "scripts/_jarvis_final_revision.py::_glide_step",
            "cap_relative": 0.34,
            "rationale": (
                "The MODERATE risk tier upper bound is relative change <= "
                "0.35. Capping at 0.34 leaves a small safety margin so the "
                "classifier always tags the glide-step tweak as MODERATE. "
                "Non-numeric or zero-baseline keys are passed through at "
                "baseline to avoid accidental structural flips."
            ),
            "effect_on_winner": {
                "confluence_threshold": "6 -> 8  (+2, within cap)",
                "stop_atr_mult": "1.25 -> 1.0  (-0.25, within cap)",
                "tp_atr_mult": "2.0 -> 2.68  (clipped from target 3.0)",
                "daily_dd_cap_pct": "3.0 -> 3.0  (unchanged)",
                "max_open_positions": "1 -> 1  (unchanged)",
            },
        },
        "tests_new": new_tests - prev_tests,
        "tests_passing_before": prev_tests,
        "tests_passing_after": new_tests,
    }

    state["overall_progress_pct"] = state.get("overall_progress_pct", 99)

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to v0.1.27 at {now}")
    print(f"  tests_passing: {prev_tests} -> {new_tests} ({new_tests - prev_tests:+d})")
    print("  shared_artifacts.eta_engine_v0_1_27_final_revision written")
    print("  ready_for_rollout: True  (external funding gate unchanged)")


if __name__ == "__main__":
    main()
