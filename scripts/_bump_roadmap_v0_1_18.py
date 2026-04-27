"""One-shot: bump roadmap_state.json to v0.1.18.

Closes out P1_BRAIN (60% -> 100%). Three in-progress tasks land:

  * htf_engine    -- brain/htf_engine.py (HtfBias + HtfEngine) with 27 tests.
                     Daily EMA + 4H structure -> top-down bias vector that
                     feeds features.trend_bias without an adapter.
  * indicator_suite -- brain/indicator_suite.py (regime-aware weighting).
                       5 regime profiles, each summing to 10.0. Thin wrapper
                       score_confluence_regime_aware() around the scorer.
                       19 new tests.
  * edge_doc      -- docs/edge_rules.md. Consolidated playbook covering HTF
                     -> regime -> indicator suite -> scorer -> bot setups
                     -> risk rails, with full feature catalog + pointers.

Adds 46 tests (775 -> 821). Keeps P9_ROLLOUT.live_tiny_size blocker note
(funding gate) intact.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"


def _find_task(phase: dict, task_id: str) -> dict:
    for t in phase["tasks"]:
        if t.get("id") == task_id:
            return t
    raise KeyError(f"task {task_id} not found in phase {phase.get('id')}")


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    sa["eta_engine_tests_passing"] = 821

    # P1_BRAIN promotion
    by_id = {p["id"]: p for p in state["phases"]}
    p1 = by_id["P1_BRAIN"]
    p1["progress_pct"] = 100

    htf = _find_task(p1, "htf_engine")
    htf["status"] = "done"
    htf["note"] = (
        "brain/htf_engine.py + 27 tests. HtfBias(daily_ema, daily_struct, "
        "h4_struct, bias, agreement). Composition: daily slope+struct must "
        "agree; 4H must agree or be neutral. context_for_trend_bias() emits "
        "the dict the TrendBiasFeature already accepts -- no adapter."
    )

    idx = _find_task(p1, "indicator_suite")
    idx["status"] = "done"
    idx["note"] = (
        "brain/indicator_suite.py + 19 tests. 5 regime profiles (TRENDING, "
        "RANGING, HIGH_VOL, LOW_VOL, CRISIS, TRANSITION), each summing to "
        "10.0. score_confluence_regime_aware() swaps weights per regime and "
        "restores defaults (even on exception). weighted_confluence_tuple() "
        "returns a per-feature contribution vector."
    )

    edge = _find_task(p1, "edge_doc")
    edge["status"] = "done"
    edge["note"] = (
        "docs/edge_rules.md. Consolidated edge playbook. Sections: "
        "philosophy, macro->entry hierarchy, HTF engine contract, regime "
        "classifier, indicator-suite table, full per-bot setup catalog "
        "(MNQ/NQ/ETH/SOL/XRP/BTC-seed), cross-bot risk rails, feature "
        "catalog, authoritative pointers, change log."
    )

    # New brain-layer shared artifact summary
    sa["eta_engine_p1_brain"] = {
        "timestamp_utc": now,
        "completed_tasks": ["htf_engine", "indicator_suite", "edge_doc"],
        "new_modules": [
            "eta_engine/brain/htf_engine.py",
            "eta_engine/brain/indicator_suite.py",
        ],
        "new_docs": ["eta_engine/docs/edge_rules.md"],
        "new_test_files": [
            "tests/test_htf_engine.py (27 tests)",
            "tests/test_indicator_suite.py (19 tests)",
        ],
        "tests_new": 46,
        "notes": (
            "HTF engine is the canonical daily+4H provider for "
            "features.trend_bias. Indicator suite makes confluence_scorer "
            "regime-adaptive without changing its public API. edge_rules.md "
            "is now the single source of truth for every setup rule."
        ),
    }

    # Overall progress: every remaining in_progress / pending phase is
    # still open, but the brain layer being 100% raises the weighted mean.
    state["overall_progress_pct"] = 99

    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"bumped roadmap_state.json to v0.1.18 at {now}")
    print("  tests_passing: 775 -> 821 (+46)")
    print("  P1_BRAIN: 60% -> 100% (htf_engine, indicator_suite, edge_doc -> done)")
    print("  overall_progress_pct: 98 -> 99")


if __name__ == "__main__":
    main()
