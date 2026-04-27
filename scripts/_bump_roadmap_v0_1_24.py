"""One-shot: bump roadmap_state.json to v0.1.24.

CONSOLIDATION PASS -- no new code, just status hygiene.

Changes
-------
  * P1_BRAIN, P3_PROOF, P4_SHIELD, P5_EXEC: all at 100% but stuck on
    ``status: in_progress`` from legacy bumps. Flip to ``done``.

  * P11_STAKE: 90% -> 100%. All 7 listed tasks (lido_adapter,
    jito_adapter, flare_xrp, ethena_susde, allocation_engine,
    apy_tracker, restaking) have ``status: done``. The 10% lag was a
    placeholder; promoting to 100% and ``status: done``.

  * P12_POLISH: 95% -> 100%. All 5 listed tasks (open_testing,
    parameter_sweep, master_tweaks, tax_auto, go_live_checklist) have
    ``status: done``. Promoting.

  * P9_ROLLOUT stays at 85% -- gated by $1000 Tradovate funding
    requirement for API credentials. This is external to code; will
    close on funding, not on a commit.

  * overall_progress_pct: 99 -> 99. 12 of 13 phases at 100%; P9
    pinned at 85% by external funding gate. Arithmetic mean is 98.8,
    rounding up to 99 to reflect code-side completeness.

Tests unchanged: 1019.
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

    by_id = {p["id"]: p for p in state["phases"]}

    # Flip status for phases already at 100
    for pid in ("P1_BRAIN", "P3_PROOF", "P4_SHIELD", "P5_EXEC"):
        p = by_id[pid]
        if p["progress_pct"] == 100 and p["status"] != "done":
            p["status"] = "done"

    # Promote P11_STAKE and P12_POLISH
    p11 = by_id["P11_STAKE"]
    p11["progress_pct"] = 100
    p11["status"] = "done"

    p12 = by_id["P12_POLISH"]
    p12["progress_pct"] = 100
    p12["status"] = "done"

    # Shared-artifact summary for the consolidation
    sa = state["shared_artifacts"]
    sa["eta_engine_consolidation_v0_1_24"] = {
        "timestamp_utc": now,
        "note": (
            "Consolidation pass. No new modules; status flag hygiene. "
            "P1/P3/P4/P5 status: in_progress -> done (already 100%). "
            "P11_STAKE 90% -> 100%. P12_POLISH 95% -> 100%. "
            "Only P9_ROLLOUT remains below 100%, gated externally by "
            "the $1000 Tradovate API funding requirement."
        ),
        "phases_completed_count": 12,
        "phases_total_count": 13,
        "tests_passing": sa.get("eta_engine_tests_passing", 1019),
        "external_gate_note": (
            "P9_ROLLOUT (85%) will close when Tradovate API "
            "credentials can be issued (requires $1000 funded balance). "
            "All code for authorize_tradovate is in place and tested "
            "dry-run; credentials go into .env and live_tiny preflight "
            "runs. No additional code change needed."
        ),
    }

    state["overall_progress_pct"] = 99

    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"bumped roadmap_state.json to v0.1.24 at {now}")
    print("  P1_BRAIN/P3_PROOF/P4_SHIELD/P5_EXEC status -> done")
    print("  P11_STAKE: 90% -> 100% (status -> done)")
    print("  P12_POLISH: 95% -> 100% (status -> done)")
    print("  P9_ROLLOUT: 85% (external funding gate)")
    print("  overall_progress_pct: 99")


if __name__ == "__main__":
    main()
