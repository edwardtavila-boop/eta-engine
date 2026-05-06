"""Read-only paper-live transition verifier.

This is the final "can we launch paper_live yet?" check after the operator
installs/logs into IB Gateway and runs the TWS watchdog. It composes existing
read-only surfaces instead of starting services or clearing holds:

* ibkr_surface_status: confirms TWS/IB Gateway API 4002 is handshake-ready.
* ibgateway_release_guard: dry-runs the hold release gate.
* operator_queue_snapshot: confirms the top JARVIS blocker is no longer OP-19.

The script never submits orders, never clears the order-entry hold, and never
starts scheduled tasks. Use ``ibgateway_release_guard --execute`` separately
only after this check reports a ready state.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.scripts import (
    ibgateway_release_guard,
    ibkr_surface_status,
    operator_queue_snapshot,
    workspace_roots,
)

_DEFAULT_OUT = workspace_roots.ETA_RUNTIME_STATE_DIR / "paper_live_transition_check.json"
_LAUNCH_COMMAND = (
    "$env:ETA_SUPERVISOR_MODE='paper_live'; "
    "python eta_engine/scripts/jarvis_strategy_supervisor.py"
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _gate(
    name: str,
    *,
    passed: bool,
    detail: str,
    next_action: str = "",
    critical: bool = True,
) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "critical": bool(critical),
        "detail": detail,
        "next_action": next_action,
    }


def _first_op_id(snapshot: dict[str, Any]) -> str:
    raw = snapshot.get("first_blocker_op_id")
    return str(raw or "")


def _blocked_count(snapshot: dict[str, Any]) -> int:
    try:
        return int(snapshot.get("blocked_count") or 0)
    except (TypeError, ValueError):
        return 0


def _paper_ready_count(snapshot: dict[str, Any]) -> int:
    try:
        return int(snapshot.get("bot_strategy_paper_ready") or 0)
    except (TypeError, ValueError):
        return 0


def _op19_next_action(queue: dict[str, Any], release_guard: dict[str, Any]) -> str:
    """Return the most actionable OP-19 recovery step for the operator."""
    queue_action = str(queue.get("first_next_action") or "")
    if _first_op_id(queue) != "OP-19":
        return queue_action

    hold = release_guard.get("hold") if isinstance(release_guard, dict) else {}
    hold_payload = hold if isinstance(hold, dict) else {}
    hold_active = bool(hold_payload.get("active"))
    hold_reason = str(hold_payload.get("reason") or "")
    if hold_active and hold_reason == "ibgateway_waiting_for_manual_login_or_2fa":
        return (
            "Complete the visible IBKR Gateway login/2FA, then run "
            "python -m eta_engine.scripts.tws_watchdog --host 127.0.0.1 --port 4002"
        )

    return queue_action


def build_transition_check(
    *,
    check_client_portal: bool = False,
    max_watchdog_age_s: int = 180,
    limit: int = 5,
) -> dict[str, Any]:
    """Return a read-only paper-live transition verdict."""
    ibkr_status = ibkr_surface_status.build_status(
        check_client_portal=check_client_portal,
    )
    release_guard = ibgateway_release_guard.run_guard(
        execute=False,
        max_watchdog_age_s=max_watchdog_age_s,
    )
    queue = operator_queue_snapshot.build_snapshot(limit=limit)

    paper_live_ready = bool(ibkr_status.get("summary", {}).get("paper_live_ready"))
    release_ready = (
        str(release_guard.get("status") or "") in {"ready_to_release", "released"}
        and release_guard.get("operator_action_required") is False
    )
    first_op = _first_op_id(queue)
    op19_clear = first_op != "OP-19"
    paper_ready = _paper_ready_count(queue)
    blockers = _blocked_count(queue)
    op19_next_action = _op19_next_action(queue, release_guard)

    gates = [
        _gate(
            "tws_api_4002",
            passed=paper_live_ready,
            detail=str(ibkr_status.get("summary", {}).get("operator_action") or ""),
            next_action="python -m eta_engine.scripts.tws_watchdog --host 127.0.0.1 --port 4002",
        ),
        _gate(
            "ibgateway_release_guard",
            passed=release_ready,
            detail=str(release_guard.get("reason") or release_guard.get("status") or ""),
            next_action="python -m eta_engine.scripts.ibgateway_release_guard",
        ),
        _gate(
            "op19_gateway_runtime",
            passed=op19_clear,
            detail=(
                "OP-19 is clear"
                if op19_clear
                else "OP-19 is still the top blocker: IB Gateway 10.46/API 4002 is not recovered"
            ),
            next_action=op19_next_action,
        ),
        _gate(
            "paper_ready_bots",
            passed=paper_ready > 0,
            detail=f"{paper_ready} bot(s) are paper-ready in the strategy readiness snapshot",
            next_action="python -m eta_engine.scripts.paper_live_launch_check --json",
        ),
    ]
    critical_ready = all(gate["passed"] for gate in gates if gate["critical"])
    if critical_ready and blockers == 0:
        status = "ready_to_launch_paper_live"
    elif critical_ready:
        status = "ready_with_operator_queue_warnings"
    else:
        status = "blocked"

    return {
        "schema_version": 1,
        "generated_at": _utc_now_iso(),
        "status": status,
        "critical_ready": critical_ready,
        "launch_command": _LAUNCH_COMMAND if critical_ready else "",
        "operator_queue_blocked_count": blockers,
        "operator_queue_first_blocker_op_id": first_op or None,
        "operator_queue_first_next_action": op19_next_action or queue.get("first_next_action"),
        "paper_ready_bots": paper_ready,
        "gates": gates,
        "ibkr_surface_status": ibkr_status,
        "release_guard": release_guard,
    }


def write_transition_check(payload: dict[str, Any], path: Path = _DEFAULT_OUT) -> Path:
    """Write ``payload`` to a canonical runtime artifact."""
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-client-portal", action="store_true")
    parser.add_argument("--max-watchdog-age-s", type=int, default=180)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--no-write", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = build_transition_check(
        check_client_portal=args.check_client_portal,
        max_watchdog_age_s=args.max_watchdog_age_s,
        limit=max(1, args.limit),
    )
    if not args.no_write:
        write_transition_check(payload, args.out)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if payload["status"] == "ready_to_launch_paper_live" else 1


if __name__ == "__main__":
    raise SystemExit(main())
