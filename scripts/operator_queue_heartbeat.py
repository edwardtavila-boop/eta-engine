"""Emit a changed-only operator queue heartbeat for automation.

The snapshot writer owns the canonical artifact. This wrapper turns its drift
block into a notification-friendly line so 10-minute wakeups can stay quiet
when the operator queue has not changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import operator_queue_snapshot, workspace_roots  # noqa: E402


def build_heartbeat(snapshot: dict[str, Any], snapshot_path: Path | None) -> dict[str, Any]:
    """Return a compact notification payload from a snapshot with drift."""
    drift = snapshot.get("drift") if isinstance(snapshot.get("drift"), dict) else {}
    changed = bool(drift.get("changed"))
    return {
        "schema_version": 1,
        "generated_at": snapshot.get("generated_at"),
        "source": "operator_queue_snapshot.drift",
        "notify": changed,
        "status": snapshot.get("status"),
        "blocked_count": int(snapshot.get("blocked_count") or 0),
        "first_blocker_op_id": snapshot.get("first_blocker_op_id"),
        "first_next_action": snapshot.get("first_next_action"),
        "bot_strategy_readiness_status": snapshot.get("bot_strategy_readiness_status"),
        "bot_strategy_blocked_data": int(snapshot.get("bot_strategy_blocked_data") or 0),
        "bot_strategy_paper_ready": int(snapshot.get("bot_strategy_paper_ready") or 0),
        "bot_strategy_can_live_any": bool(snapshot.get("bot_strategy_can_live_any")),
        "drift_changed": changed,
        "drift_summary": drift.get("summary"),
        "changed_fields": drift.get("changed_fields") or [],
        "blocked_count_delta": drift.get("blocked_count_delta"),
        "bot_strategy_blocked_data_delta": drift.get("bot_strategy_blocked_data_delta"),
        "snapshot_path": str(snapshot_path) if snapshot_path is not None else None,
    }


def render_text(heartbeat: dict[str, Any]) -> str:
    """Return a single log line suitable for scheduler output."""
    first = heartbeat.get("first_blocker_op_id") or "none"
    action = heartbeat.get("first_next_action") or "none"
    notify = "yes" if heartbeat.get("notify") else "no"
    fields = ",".join(heartbeat.get("changed_fields") or []) or "none"
    bot_status = heartbeat.get("bot_strategy_readiness_status") or "unknown"
    bot_blocked = heartbeat.get("bot_strategy_blocked_data")
    bot_paper = heartbeat.get("bot_strategy_paper_ready")
    return (
        "operator_queue_heartbeat "
        f"notify={notify} status={heartbeat.get('status')} "
        f"blocked={heartbeat.get('blocked_count')} first={first} "
        f"bot_readiness={bot_status} bot_blocked_data={bot_blocked} "
        f"bot_paper_ready={bot_paper} changed_fields={fields} next={action} "
        f"drift={heartbeat.get('drift_summary') or 'none'}"
    )


def build_snapshot_with_drift(
    *,
    out_path: Path,
    previous_path: Path | None,
    limit: int,
) -> dict[str, Any]:
    """Build the operator queue snapshot and attach its drift block."""
    resolved_previous_path = previous_path or operator_queue_snapshot.default_previous_path_for(out_path)
    snapshot = operator_queue_snapshot.build_snapshot(limit=max(1, limit))
    previous = operator_queue_snapshot.load_snapshot(out_path) or operator_queue_snapshot.load_snapshot(
        resolved_previous_path
    )
    snapshot["drift"] = operator_queue_snapshot.compare_snapshots(previous, snapshot)
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="operator_queue_heartbeat")
    parser.add_argument("--out", type=Path, default=workspace_roots.ETA_OPERATOR_QUEUE_SNAPSHOT_PATH)
    parser.add_argument("--previous", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="print JSON heartbeat payload")
    parser.add_argument(
        "--changed-only",
        action="store_true",
        help="suppress output when the queue did not drift since the prior snapshot",
    )
    parser.add_argument("--no-write", action="store_true", help="build and print without writing the snapshot")
    parser.add_argument("--strict-blockers", action="store_true", help="exit 2 when blockers are present")
    parser.add_argument("--strict-drift", action="store_true", help="exit 3 when operator queue drift is detected")
    args = parser.parse_args(argv)

    previous_path = args.previous or operator_queue_snapshot.default_previous_path_for(args.out)
    snapshot = build_snapshot_with_drift(out_path=args.out, previous_path=previous_path, limit=args.limit)
    written_path = (
        None
        if args.no_write
        else operator_queue_snapshot.write_snapshot(snapshot, args.out, previous_path=previous_path)
    )
    heartbeat = build_heartbeat(snapshot, written_path)

    if not args.changed_only or heartbeat["notify"]:
        if args.json:
            print(json.dumps(heartbeat, indent=2, sort_keys=True, default=str))
        else:
            print(render_text(heartbeat))

    if args.strict_drift and heartbeat["notify"]:
        return 3
    if args.strict_blockers and int(heartbeat.get("blocked_count") or 0) > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
