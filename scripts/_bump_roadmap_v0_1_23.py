"""One-shot: bump roadmap_state.json to v0.1.23.

Closes out P7_OPS (95% -> 100%). One task lands:

  * vps_redundancy -- obs/vps_redundancy.py (failover controller with
                      injectable HealthProbe + DnsSwitchProvider
                      protocols; FailoverPolicy with fast-fail-over /
                      slow-fail-back thresholds; secondary_degraded
                      detection; VpsRedundancyController runs the
                      probe -> decide -> DNS switch loop). 29 tests.

Adds 29 tests (990 -> 1019).
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
    sa["eta_engine_tests_passing"] = 1019

    by_id = {p["id"]: p for p in state["phases"]}
    p7 = by_id["P7_OPS"]
    p7["progress_pct"] = 100
    p7["status"] = "done"

    vr = _find_task(p7, "vps_redundancy")
    vr["status"] = "done"
    vr["note"] = (
        "obs/vps_redundancy.py + 29 tests. Injectable-protocol failover: "
        "HealthProbe Protocol (probe(role) -> HealthSnapshot) + "
        "DnsSwitchProvider Protocol (switch(to_role, reason)). "
        "Stub implementations for tests/dry-run. FailoverPolicy gates: "
        "primary_unhealthy_threshold=3 (fast fail-over), "
        "primary_recovery_threshold=10 (slow fail-back, avoids flap), "
        "secondary_unhealthy_threshold=5 (pages operator when both are "
        "blind), degraded_counts_as_unhealthy=True (can be disabled to "
        "only flip on full DOWN). FailoverController is pure-decision "
        "(no I/O), deque-backed snapshot history, consecutive-tail "
        "counting. VpsRedundancyController wraps probe + decide + DNS "
        "switch for production loop; probe exceptions recorded as DOWN "
        "snapshots; DNS-switch exceptions recorded on the FailoverEvent "
        "but do not crash the controller."
    )

    # New P7_OPS shared artifact summary
    sa["eta_engine_p7_ops"] = {
        "timestamp_utc": now,
        "new_module": "eta_engine/obs/vps_redundancy.py",
        "new_test_file": "tests/test_vps_redundancy.py (29 tests)",
        "tests_new": 29,
        "protocols": {
            "HealthProbe": "async probe(role: VpsRole) -> HealthSnapshot",
            "DnsSwitchProvider": "async switch(to_role, reason) -> None",
        },
        "policy_defaults": {
            "primary_unhealthy_threshold": 3,
            "primary_recovery_threshold": 10,
            "secondary_unhealthy_threshold": 5,
            "degraded_counts_as_unhealthy": True,
        },
        "safety_guards": [
            "Fast fail-over (3 probes) / slow fail-back (10 probes) prevents flap",
            "Probe exceptions caught and recorded as DOWN snapshots",
            "DNS switch exceptions caught; FailoverEvent.dns_error is set but controller does not crash",
            "secondary_degraded() signals blind-redundancy condition for operator paging",
            "FailoverPolicy validates thresholds are >= 1",
        ],
        "notes": (
            "This module assumes two VPSes are already running the "
            "stack; the controller's job is orchestration, not "
            "provisioning. No real DNS/cloud-API coupling -- "
            "production wires a DnsSwitchProvider for Cloudflare, "
            "Route53, etc."
        ),
    }

    # Overall rolls up when all phases done. P7 was the last sub-100
    # besides the Tradovate-funding-gated P9_ROLLOUT + P11/P12 which
    # are cosmetic percentage lag. Leave overall at 99 until we make
    # a consolidation pass.
    state["overall_progress_pct"] = 99

    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"bumped roadmap_state.json to v0.1.23 at {now}")
    print("  tests_passing: 990 -> 1019 (+29)")
    print("  P7_OPS: 95% -> 100% (vps_redundancy -> done)")
    print("  overall_progress_pct: 99")


if __name__ == "__main__":
    main()
