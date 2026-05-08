"""Write a canonical operator-blocker snapshot for automation.

The dashboard can read live status through the API, but heartbeat automation
needs a cheap file artifact it can diff between wakeups without starting a
server. This command is broker-safe: it only calls the existing read-only
operator queue summary and writes the result under the canonical workspace
state directory by default.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import jarvis_status, workspace_roots  # noqa: E402


def load_snapshot(path: Path) -> dict[str, Any] | None:
    """Load a prior snapshot, returning None when absent or unreadable."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def compare_snapshots(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    """Return a compact drift summary between two snapshot payloads."""
    if previous is None:
        return {
            "previous_present": False,
            "changed": True,
            "changed_fields": ["baseline_missing"],
            "blocked_count_delta": None,
            "launch_blocked_count_delta": None,
            "status_changed": None,
            "launch_status_changed": None,
            "first_blocker_changed": None,
            "first_launch_blocker_changed": None,
            "first_next_action_changed": None,
            "first_launch_next_action_changed": None,
            "summary": "no previous operator queue snapshot",
        }

    changed_fields: list[str] = []
    blocked_count_delta = int(current.get("blocked_count") or 0) - int(previous.get("blocked_count") or 0)
    has_launch_blocked_count = "launch_blocked_count" in previous and "launch_blocked_count" in current
    launch_blocked_count_delta = (
        int(current.get("launch_blocked_count") or 0) - int(previous.get("launch_blocked_count") or 0)
        if has_launch_blocked_count
        else 0
    )
    status_changed = current.get("status") != previous.get("status")
    launch_status_changed = current.get("launch_status") != previous.get("launch_status")
    first_blocker_changed = current.get("first_blocker_op_id") != previous.get("first_blocker_op_id")
    first_launch_blocker_changed = current.get("first_launch_blocker_op_id") != previous.get(
        "first_launch_blocker_op_id"
    )
    first_next_action_changed = current.get("first_next_action") != previous.get("first_next_action")
    first_launch_next_action_changed = current.get("first_launch_next_action") != previous.get(
        "first_launch_next_action"
    )
    bot_readiness_status_changed = current.get("bot_strategy_readiness_status") != previous.get(
        "bot_strategy_readiness_status"
    )
    bot_blocked_data_delta = (
        int(current.get("bot_strategy_blocked_data") or 0)
        - int(previous.get("bot_strategy_blocked_data") or 0)
    )
    if blocked_count_delta:
        changed_fields.append("blocked_count")
    if has_launch_blocked_count and launch_blocked_count_delta:
        changed_fields.append("launch_blocked_count")
    if status_changed:
        changed_fields.append("status")
    if launch_status_changed and ("launch_status" in previous and "launch_status" in current):
        changed_fields.append("launch_status")
    if first_blocker_changed:
        changed_fields.append("first_blocker_op_id")
    if (
        first_launch_blocker_changed
        and (
            "first_launch_blocker_op_id" in previous
            and "first_launch_blocker_op_id" in current
        )
    ):
        changed_fields.append("first_launch_blocker_op_id")
    if first_next_action_changed:
        changed_fields.append("first_next_action")
    if (
        first_launch_next_action_changed
        and (
            "first_launch_next_action" in previous
            and "first_launch_next_action" in current
        )
    ):
        changed_fields.append("first_launch_next_action")
    if (
        bot_readiness_status_changed
        and (
            "bot_strategy_readiness_status" in previous
            or "bot_strategy_readiness_status" in current
        )
    ):
        changed_fields.append("bot_strategy_readiness_status")
    if (
        bot_blocked_data_delta
        and (
            "bot_strategy_blocked_data" in previous
            or "bot_strategy_blocked_data" in current
        )
    ):
        changed_fields.append("bot_strategy_blocked_data")
    changed = bool(changed_fields)
    summary = (
        "operator queue drift detected: " + ", ".join(changed_fields)
        if changed
        else "operator queue unchanged"
    )
    return {
        "previous_present": True,
        "changed": changed,
        "changed_fields": changed_fields,
        "blocked_count_delta": blocked_count_delta,
        "launch_blocked_count_delta": launch_blocked_count_delta,
        "status_changed": status_changed,
        "launch_status_changed": launch_status_changed,
        "first_blocker_changed": first_blocker_changed,
        "first_launch_blocker_changed": first_launch_blocker_changed,
        "first_next_action_changed": first_next_action_changed,
        "first_launch_next_action_changed": first_launch_next_action_changed,
        "bot_strategy_readiness_status_changed": bot_readiness_status_changed,
        "bot_strategy_blocked_data_delta": bot_blocked_data_delta,
        "previous": {
            "status": previous.get("status"),
            "launch_status": previous.get("launch_status"),
            "blocked_count": previous.get("blocked_count"),
            "launch_blocked_count": previous.get("launch_blocked_count"),
            "first_blocker_op_id": previous.get("first_blocker_op_id"),
            "first_launch_blocker_op_id": previous.get("first_launch_blocker_op_id"),
            "first_next_action": previous.get("first_next_action"),
            "first_launch_next_action": previous.get("first_launch_next_action"),
            "bot_strategy_readiness_status": previous.get("bot_strategy_readiness_status"),
            "bot_strategy_blocked_data": previous.get("bot_strategy_blocked_data"),
            "generated_at": previous.get("generated_at"),
        },
        "summary": summary,
    }


def default_previous_path_for(path: Path) -> Path:
    """Return the previous-snapshot path paired with ``path``."""
    if path == workspace_roots.ETA_OPERATOR_QUEUE_SNAPSHOT_PATH:
        return workspace_roots.ETA_OPERATOR_QUEUE_PREVIOUS_SNAPSHOT_PATH
    return path.with_name(f"{path.stem}.previous{path.suffix}")


def _readiness_status(readiness: dict[str, Any], blocked_data: int) -> str:
    raw_status = str(readiness.get("status") or "unknown")
    if raw_status != "ready":
        return "degraded"
    return "blocked" if blocked_data > 0 else "ready"


def _readiness_summary(readiness: dict[str, Any]) -> tuple[int, int, bool]:
    summary = readiness.get("summary") if isinstance(readiness, dict) else {}
    summary_payload = summary if isinstance(summary, dict) else {}
    lanes = summary_payload.get("launch_lanes")
    lane_payload = lanes if isinstance(lanes, dict) else {}
    blocked_data = int(summary_payload.get("blocked_data") or lane_payload.get("blocked_data") or 0)
    paper_ready = int(summary_payload.get("can_paper_trade") or 0)
    can_live_any = bool(summary_payload.get("can_live_any"))
    return blocked_data, paper_ready, can_live_any


def _readiness_top_actions(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, object]]:
    lane_rank = {
        "blocked_data": 0,
        "live_preflight": 1,
        "paper_soak": 2,
        "research": 3,
        "shadow_only": 4,
        "non_edge": 5,
        "deactivated": 6,
    }
    top_actions: list[dict[str, object]] = []
    for row in sorted(
        (item for item in rows if item.get("next_action")),
        key=lambda item: (lane_rank.get(str(item.get("launch_lane") or ""), 7), str(item.get("bot_id") or "")),
    ):
        if len(top_actions) >= max(0, limit):
            break
        top_actions.append(
            {
                "bot_id": row.get("bot_id"),
                "strategy_id": row.get("strategy_id"),
                "launch_lane": row.get("launch_lane"),
                "data_status": row.get("data_status"),
                "promotion_status": row.get("promotion_status"),
                "can_paper_trade": bool(row.get("can_paper_trade")),
                "can_live_trade": bool(row.get("can_live_trade")),
                "next_action": row.get("next_action"),
            }
        )
    return top_actions


def _build_current_readiness_summary(*, limit: int) -> dict[str, Any]:
    """Build a fresh supervisor-pinned readiness payload for launch surfaces.

    The cached ``bot_strategy_readiness_latest.json`` can lag the active
    supervisor pin when strategy promotion changes land between scheduled
    refreshes. Operator snapshots drive paper-live cards, so prefer a fresh
    supervisor-pinned matrix there and fall back to the cached summary only if
    the live build fails.
    """
    try:
        from eta_engine.scripts import bot_strategy_readiness

        supervisor_pinned = bot_strategy_readiness.supervisor_pinned_bot_ids()
        rows = bot_strategy_readiness.build_readiness_matrix(bot_ids=list(supervisor_pinned))
        snapshot = bot_strategy_readiness.build_snapshot(
            rows,
            scope="supervisor_pinned",
            supervisor_pinned=supervisor_pinned,
        )
        row_payloads = [dict(item) for item in snapshot.get("rows", []) if isinstance(item, dict)]
        rows_by_bot = {
            bot_id: row
            for row in row_payloads
            if (bot_id := str(row.get("bot_id") or row.get("id") or row.get("name") or "").strip())
        }
        return {
            "source": "bot_strategy_readiness",
            "path": str(workspace_roots.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH),
            "status": "ready",
            "schema_version": snapshot.get("schema_version"),
            "generated_at": snapshot.get("generated_at"),
            "scope": snapshot.get("scope"),
            "supervisor_pinned": snapshot.get("supervisor_pinned") or [],
            "summary": snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {},
            "row_count": len(row_payloads),
            "rows": row_payloads,
            "rows_by_bot": rows_by_bot,
            "top_actions": _readiness_top_actions(row_payloads, limit=limit),
            "cache_refresh": "fresh_supervisor_pinned",
        }
    except Exception as exc:  # noqa: BLE001 - snapshot must fail soft for ops.
        payload = jarvis_status.build_bot_strategy_readiness_summary(limit=limit)
        payload = dict(payload) if isinstance(payload, dict) else {}
        payload["cache_refresh"] = "cached_fallback"
        payload["refresh_error"] = str(exc)
        return payload


def build_snapshot(*, limit: int = 5, refresh_readiness: bool = False) -> dict[str, Any]:
    """Return the canonical automation snapshot payload."""
    queue = jarvis_status.build_operator_queue_summary(limit=limit)
    readiness = (
        _build_current_readiness_summary(limit=limit)
        if refresh_readiness
        else jarvis_status.build_bot_strategy_readiness_summary(limit=limit)
    )
    summary = queue.get("summary") if isinstance(queue, dict) else {}
    top_blockers = queue.get("top_blockers") if isinstance(queue, dict) else []
    top_launch_blockers = queue.get("top_launch_blockers") if isinstance(queue, dict) else []
    next_actions = queue.get("next_actions") if isinstance(queue, dict) else []
    launch_next_actions = queue.get("launch_next_actions") if isinstance(queue, dict) else []
    blocked = int(summary.get("BLOCKED", 0)) if isinstance(summary, dict) else 0
    raw_launch_blocked = queue.get("launch_blocked_count") if isinstance(queue, dict) else None
    try:
        launch_blocked = int(raw_launch_blocked) if raw_launch_blocked is not None else blocked
    except (TypeError, ValueError):
        launch_blocked = blocked
    first_blocker = top_blockers[0] if isinstance(top_blockers, list) and top_blockers else {}
    first_launch_blocker = (
        top_launch_blockers[0]
        if isinstance(top_launch_blockers, list) and top_launch_blockers
        else (first_blocker if launch_blocked > 0 else {})
    )
    first_op_id = first_blocker.get("op_id") if isinstance(first_blocker, dict) else None
    first_launch_op_id = first_launch_blocker.get("op_id") if isinstance(first_launch_blocker, dict) else None
    first_launch_next_action = (
        launch_next_actions[0]
        if isinstance(launch_next_actions, list) and launch_next_actions
        else (
            next_actions[0]
            if launch_blocked > 0 and isinstance(next_actions, list) and next_actions
            else None
        )
    )
    bot_blocked_data, bot_paper_ready, bot_can_live_any = _readiness_summary(readiness)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "jarvis_status.operator_queue",
        "status": "blocked" if blocked else "clear",
        "blocked_count": blocked,
        "first_blocker_op_id": first_op_id,
        "first_next_action": next_actions[0] if isinstance(next_actions, list) and next_actions else None,
        "launch_status": "blocked" if launch_blocked else "clear",
        "launch_blocked_count": launch_blocked,
        "non_launch_blocked_count": max(blocked - launch_blocked, 0),
        "first_launch_blocker_op_id": first_launch_op_id,
        "first_launch_next_action": first_launch_next_action,
        "bot_strategy_readiness_status": _readiness_status(readiness, bot_blocked_data),
        "bot_strategy_blocked_data": bot_blocked_data,
        "bot_strategy_paper_ready": bot_paper_ready,
        "bot_strategy_can_live_any": bot_can_live_any,
        "bot_strategy_readiness": readiness,
        "operator_queue": queue,
    }


def write_snapshot(
    snapshot: dict[str, Any],
    path: Path = workspace_roots.ETA_OPERATOR_QUEUE_SNAPSHOT_PATH,
    *,
    previous_path: Path | None = workspace_roots.ETA_OPERATOR_QUEUE_PREVIOUS_SNAPSHOT_PATH,
) -> Path:
    """Atomically write ``snapshot`` to ``path`` and return the path."""
    workspace_roots.ensure_parent(path)
    if previous_path is not None and path.exists():
        workspace_roots.ensure_parent(previous_path)
        previous_tmp = previous_path.with_suffix(previous_path.suffix + ".tmp")
        previous_tmp.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        previous_tmp.replace(previous_path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _render_text(snapshot: dict[str, Any], path: Path | None) -> str:
    """Return a compact human line for logs and heartbeats."""
    target = f" -> {path}" if path is not None else ""
    first = snapshot.get("first_blocker_op_id") or "none"
    action = snapshot.get("first_next_action") or "none"
    launch_blocked = snapshot.get("launch_blocked_count")
    bot_status = snapshot.get("bot_strategy_readiness_status") or "unknown"
    bot_blocked = snapshot.get("bot_strategy_blocked_data")
    bot_paper = snapshot.get("bot_strategy_paper_ready")
    drift = snapshot.get("drift") if isinstance(snapshot.get("drift"), dict) else {}
    drift_line = drift.get("summary") if isinstance(drift, dict) else None
    return (
        f"operator_queue_snapshot status={snapshot['status']} "
        f"blocked={snapshot['blocked_count']} launch_blocked={launch_blocked} first={first} next={action}"
        f" bot_readiness={bot_status} bot_blocked_data={bot_blocked} bot_paper_ready={bot_paper}"
        f"{f' drift={drift_line}' if drift_line else ''}{target}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="operator_queue_snapshot")
    parser.add_argument("--out", type=Path, default=workspace_roots.ETA_OPERATOR_QUEUE_SNAPSHOT_PATH)
    parser.add_argument("--previous", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="print JSON payload")
    parser.add_argument("--no-write", action="store_true", help="build and print without writing the artifact")
    parser.add_argument(
        "--cached-readiness",
        action="store_true",
        help="use the cached bot_strategy_readiness_latest.json instead of rebuilding supervisor-pinned readiness",
    )
    parser.add_argument("--strict", action="store_true", help="exit 2 when blockers are present")
    args = parser.parse_args(argv)

    snapshot = build_snapshot(limit=max(1, args.limit), refresh_readiness=not args.cached_readiness)
    previous_path = args.previous or default_previous_path_for(args.out)
    previous = load_snapshot(args.out) or load_snapshot(previous_path)
    snapshot["drift"] = compare_snapshots(previous, snapshot)
    written_path = None if args.no_write else write_snapshot(snapshot, args.out, previous_path=previous_path)
    if args.json:
        print(json.dumps(snapshot, indent=2, sort_keys=True, default=str))
    else:
        print(_render_text(snapshot, written_path))
    return 2 if args.strict and int(snapshot.get("blocked_count") or 0) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
