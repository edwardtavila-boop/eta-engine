"""One-shot: bump roadmap_state.json to v0.1.26.

JARVIS CONTEXT v2 ENRICHMENT -- no new phase, additive on top of
the v0.1.25 Jarvis bundle. This reconciles the tests_passing count
from 1228 -> 1285 (+57) after the v2 enrichment landed in
``brain/jarvis_context.py`` (1195 lines) and its new test file
``tests/test_jarvis_context_v2.py`` (57 tests).

What the v2 enrichment adds
---------------------------
  * StressScore: composite 0..1 stress index with weighted components
    (drawdown, macro, regime, override, concentration) and a
    binding_constraint = argmax(weights).
  * SessionPhase: America/New_York session-phase classifier
    (PRE_MARKET / OPEN_AUCTION / MORNING / MIDDAY / AFTERNOON / CLOSE_AUCTION).
  * SizingHint: monotonic sizing curve conditioned on stress + action tier.
  * JarvisAlert + AlertLevel ladder: factor-specific alert escalation.
  * JarvisMargins: margin arithmetic (used vs free capacity).
  * JarvisMemory + TrajectoryState + Trajectory:
    IMPROVING / FLAT / WORSENING / UNKNOWN classification of recent
    snapshots. Engine guarantees trajectory is computed BEFORE append.
  * JarvisContextEngine: orchestrates builder + memory + alerts + sizing.
  * build_explanation / build_playbook: human-readable narrative and
    ranked action playbook from a JarvisContext.

Reconciliation
--------------
  * tests_passing: 1228 -> 1285 (+57)
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
    sa["eta_engine_tests_passing"] = 1285

    sa["eta_engine_jarvis_context_v2"] = {
        "timestamp_utc": now,
        "module": "eta_engine/brain/jarvis_context.py (1195 lines)",
        "new_test_file": "tests/test_jarvis_context_v2.py (57 tests)",
        "tests_new": 57,
        "enrichments": [
            "StressScore composite with binding_constraint = argmax(weights)",
            "SessionPhase classifier (America/New_York)",
            "SizingHint monotonic in stress and action tier",
            "JarvisAlert + AlertLevel ladder per factor",
            "JarvisMargins arithmetic (used vs free capacity)",
            "JarvisMemory + TrajectoryState classification",
            "JarvisContextEngine: builder + memory + alerts + sizing",
            "build_explanation: narrative string for a JarvisContext",
            "build_playbook: ranked action list from a JarvisContext",
        ],
        "stress_components": [
            "drawdown",
            "macro",
            "regime",
            "override",
            "concentration",
        ],
        "trajectory_states": [
            "IMPROVING",
            "FLAT",
            "WORSENING",
            "UNKNOWN",
        ],
        "invariants_tested": [
            "STRESS_WEIGHTS sums to 1.0",
            "binding_constraint = argmax(weighted component scores)",
            "sizing_hint monotonically non-increasing in stress",
            "alert levels escalate in dd/macro/regime order",
            "engine computes trajectory BEFORE appending snapshot",
            "session-phase boundaries correct at 09:30 / 16:00 ET",
        ],
    }

    # Overall stays at 99% -- no phase change.
    state["overall_progress_pct"] = state.get("overall_progress_pct", 99)

    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"bumped roadmap_state.json to v0.1.26 at {now}")
    print("  tests_passing: 1228 -> 1285 (+57)")
    print("  no phase change; P9_ROLLOUT still 85% (funding-blocked)")


if __name__ == "__main__":
    main()
