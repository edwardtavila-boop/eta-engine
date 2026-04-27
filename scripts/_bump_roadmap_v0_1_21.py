"""One-shot: bump roadmap_state.json to v0.1.21.

Closes out P2_FUEL (88% -> 100%). One task lands:

  * l2_capture -- core/l2_capture.py (L2OrderBookState + L2Update +
                  L2CaptureSink + microstructure metrics) with 32 tests.
                  Venue-agnostic reducer: wire-format-translated updates
                  funnel through ``apply_snapshot`` / ``apply_delta``
                  into a mutable dict-backed book. Strict sequence
                  guarding, crossed-book detection, and a ring-buffer
                  sink with CSV audit export.

Adds 32 tests (896 -> 928).
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
    sa["eta_engine_tests_passing"] = 928

    by_id = {p["id"]: p for p in state["phases"]}
    p2 = by_id["P2_FUEL"]
    p2["progress_pct"] = 100
    p2["status"] = "done"

    l2 = _find_task(p2, "l2_capture")
    l2["status"] = "done"
    l2["note"] = (
        "core/l2_capture.py + 32 tests. Venue-agnostic L2 reducer: "
        "L2Update (SNAPSHOT | DELTA) -> L2OrderBookState -> L2Snapshot. "
        "apply_snapshot replaces; apply_delta merges (qty=0 removes, "
        "qty>0 upserts). Strict sequence guard -- SequenceGapError on "
        "non-contiguous seq, silent skip on stale/duplicate. "
        "CrossedBookError on bb >= ba after any update. "
        "Metrics: best_bid/ask, spread, mid, weighted_mid (microprice), "
        "imbalance(k), depth(k, side), notional_depth(k, side). "
        "L2CaptureSink ring buffer + 12-column CSV audit export "
        "(ts, symbol, bbo, qty, spread, mid, weighted_mid, imbalance, "
        "top5 depth each side)."
    )

    # New data-stack shared artifact summary
    sa["eta_engine_p2_fuel"] = {
        "timestamp_utc": now,
        "new_module": "eta_engine/core/l2_capture.py",
        "new_test_file": "tests/test_l2_capture.py (32 tests)",
        "tests_new": 32,
        "update_semantics": {
            "SNAPSHOT": "replace entire book; zero-qty levels dropped",
            "DELTA": "merge: qty=0 removes level, qty>0 upserts",
        },
        "safety_guards": [
            "SequenceGapError on non-contiguous seq (strict_sequence=True)",
            "CrossedBookError if best_bid >= best_ask after update",
            "Symbol mismatch raises ValueError",
            "L2Update rejects price<=0 and qty<0",
        ],
        "metrics": [
            "spread",
            "mid",
            "weighted_mid",
            "microprice",
            "imbalance(k)",
            "depth(k, side)",
            "notional_depth(k, side)",
        ],
        "csv_columns": [
            "ts",
            "symbol",
            "best_bid",
            "best_bid_qty",
            "best_ask",
            "best_ask_qty",
            "spread",
            "mid",
            "weighted_mid",
            "imbalance_top5",
            "depth_bid_top5",
            "depth_ask_top5",
        ],
        "notes": (
            "Zero venue dependencies in this module; bybit_ws.py / "
            "tradovate_ws.py are expected to construct L2Update objects "
            "and call book.apply(). Book stored as two dicts "
            "(price->qty) for O(1) delta, sorted-at-read for snapshot."
        ),
    }

    # P2 done. overall stays at 99 (unchanged; weighted-phase math).
    state["overall_progress_pct"] = 99

    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"bumped roadmap_state.json to v0.1.21 at {now}")
    print("  tests_passing: 896 -> 928 (+32)")
    print("  P2_FUEL: 88% -> 100% (l2_capture -> done)")
    print("  overall_progress_pct: 99")


if __name__ == "__main__":
    main()
