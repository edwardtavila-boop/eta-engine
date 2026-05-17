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
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import (  # noqa: E402
    ibgateway_release_guard,
    ibkr_surface_status,
    operator_queue_snapshot,
    workspace_roots,
)

_DEFAULT_OUT = workspace_roots.ETA_RUNTIME_STATE_DIR / "paper_live_transition_check.json"
_IBKR_SUBSCRIPTION_LOG = workspace_roots.ETA_RUNTIME_LOG_DIR / "ibkr_subscription_status.jsonl"
_TRADE_CLOSES_PATH = workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH
_LAUNCH_COMMAND = "$env:ETA_SUPERVISOR_MODE='paper_live'; python eta_engine/scripts/jarvis_strategy_supervisor.py"
_LOCAL_TZ = ZoneInfo("America/New_York")
_CADENCE_WATCH_START_MINUTE_ET = 10 * 60
_CADENCE_WATCH_END_MINUTE_ET = 16 * 60 + 15
_NON_AUTHORITATIVE_GATEWAY_STATUSES = {
    "blocked_non_authoritative_gateway_host",
    "non_authoritative_gateway_host",
}
_VPS_TWS_WATCHDOG_ACTION = (
    "On the VPS only: python -m eta_engine.scripts.tws_watchdog --host 127.0.0.1 --port 4002"
)
_VPS_RELEASE_GUARD_ACTION = "On the VPS only: python -m eta_engine.scripts.ibgateway_release_guard"
_VPS_REAUTH_ACTION = "On the VPS only: python -m eta_engine.scripts.ibgateway_reauth_controller --execute"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _latest_order_api_status(
    path: Path = _IBKR_SUBSCRIPTION_LOG,
    *,
    max_age_s: int = 900,
) -> dict[str, Any] | None:
    """Return fresh order-entry evidence from the IBKR subscription verifier, if present."""
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError):
        return None
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        order_api = payload.get("order_api")
        if not isinstance(order_api, dict):
            return None
        ts_raw = str(payload.get("ts") or "")
        age_s: float | None = None
        if ts_raw:
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                age_s = (datetime.now(UTC) - ts).total_seconds()
            except ValueError:
                age_s = None
        if age_s is not None and age_s > max_age_s:
            return None
        result = dict(order_api)
        result["source"] = str(path)
        if age_s is not None:
            result["source_age_s"] = round(age_s, 3)
        return result
    return None


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


def _first_launch_op_id(snapshot: dict[str, Any]) -> str:
    if "launch_blocked_count" in snapshot:
        try:
            if int(snapshot.get("launch_blocked_count") or 0) <= 0:
                return ""
        except (TypeError, ValueError):
            return ""
    raw = snapshot.get("first_launch_blocker_op_id")
    if raw:
        return str(raw)
    return _first_op_id(snapshot)


def _blocked_count(snapshot: dict[str, Any]) -> int:
    try:
        return int(snapshot.get("blocked_count") or 0)
    except (TypeError, ValueError):
        return 0


def _launch_blocked_count(snapshot: dict[str, Any]) -> int:
    try:
        raw = snapshot.get("launch_blocked_count")
        if raw is None:
            return _blocked_count(snapshot)
        return int(raw or 0)
    except (TypeError, ValueError):
        return _blocked_count(snapshot)


def _iter_queue_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return visible operator queue items from all known snapshot shapes."""
    queue = snapshot.get("operator_queue")
    groups: list[Any] = []
    if isinstance(queue, list):
        groups.append(queue)
    elif isinstance(queue, dict):
        groups.extend([queue.get("top_launch_blockers"), queue.get("top_blockers")])

    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            marker = id(item)
            if marker in seen:
                continue
            seen.add(marker)
            items.append(item)
    return items


def _gateway_authority_is_blocked(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("non_authoritative_gateway_host") is True:
        return True
    if str(payload.get("status") or "") in _NON_AUTHORITATIVE_GATEWAY_STATUSES:
        return True
    authority = payload.get("gateway_authority")
    return isinstance(authority, dict) and authority.get("allowed") is False


def _non_authoritative_gateway_host(queue: dict[str, Any], release_guard: dict[str, Any]) -> bool:
    """Return true when recovery guidance must be scoped to the 24/7 VPS."""
    if _gateway_authority_is_blocked(release_guard):
        return True
    for item in _iter_queue_items(queue):
        if _gateway_authority_is_blocked(item):
            return True
        evidence = item.get("evidence")
        if _gateway_authority_is_blocked(evidence):
            return True
        if isinstance(evidence, dict) and _gateway_authority_is_blocked(evidence.get("reauth")):
            return True
    return False


def _paper_ready_count(snapshot: dict[str, Any]) -> int:
    try:
        return int(snapshot.get("bot_strategy_paper_ready") or 0)
    except (TypeError, ValueError):
        return 0


def _op19_next_action(
    queue: dict[str, Any],
    release_guard: dict[str, Any],
    *,
    non_authoritative_gateway_host: bool = False,
) -> str:
    """Return the most actionable OP-19 recovery step for the operator."""
    queue_action = str(queue.get("first_launch_next_action") or queue.get("first_next_action") or "")
    if _first_launch_op_id(queue) != "OP-19":
        return ""
    if non_authoritative_gateway_host:
        return queue_action if queue_action.startswith("On the VPS only:") else _VPS_REAUTH_ACTION

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


def _first_launch_detail(snapshot: dict[str, Any]) -> str:
    launch_op_id = _first_launch_op_id(snapshot)
    if not launch_op_id:
        return ""
    for item in _iter_queue_items(snapshot):
        if str(item.get("op_id") or "") == launch_op_id:
            return str(item.get("detail") or "")
    return ""


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _close_timestamp(row: dict[str, Any]) -> datetime | None:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    for key in ("ts", "closed_at", "exit_ts", "timestamp", "time"):
        parsed = _parse_datetime(row.get(key))
        if parsed is not None:
            return parsed
    if isinstance(extra, dict):
        for key in ("ts", "closed_at", "exit_ts", "timestamp", "time"):
            parsed = _parse_datetime(extra.get(key))
            if parsed is not None:
                return parsed
    return None


def _load_trade_closes_for_local_day(path: Path, *, now: datetime) -> tuple[int, datetime | None, bool]:
    local_day = now.astimezone(_LOCAL_TZ).date()
    count = 0
    latest: datetime | None = None
    exists = path.exists()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return 0, None, exists

    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        ts = _close_timestamp(row)
        if ts is None or ts.astimezone(_LOCAL_TZ).date() != local_day:
            continue
        count += 1
        if latest is None or ts > latest:
            latest = ts
    return count, latest, exists


def _cadence_watch_window(now: datetime) -> dict[str, Any]:
    local_now = now.astimezone(_LOCAL_TZ)
    minute = local_now.hour * 60 + local_now.minute
    active = (
        local_now.weekday() < 5
        and _CADENCE_WATCH_START_MINUTE_ET <= minute <= _CADENCE_WATCH_END_MINUTE_ET
    )
    return {
        "active": active,
        "timezone": "America/New_York",
        "local_date": local_now.date().isoformat(),
        "local_time": local_now.strftime("%H:%M"),
        "start": "10:00",
        "end": "16:15",
    }


def _paper_live_cadence_payload(
    *,
    ready_for_cadence_watch: bool,
    now: datetime,
    trade_closes_path: Path,
    min_daily_closes: int = 1,
) -> dict[str, Any]:
    watch_window = _cadence_watch_window(now)
    today_count, latest_close_ts, ledger_exists = _load_trade_closes_for_local_day(trade_closes_path, now=now)
    payload: dict[str, Any] = {
        "status": "not_ready",
        "passed": True,
        "critical": False,
        "today_close_count": today_count,
        "min_daily_closes": int(min_daily_closes),
        "ledger_exists": ledger_exists,
        "trade_closes_path": str(trade_closes_path),
        "watch_window": watch_window,
        "latest_close_ts": latest_close_ts.isoformat() if latest_close_ts else None,
        "detail": "cadence watch waits until paper-live readiness is green",
        "next_action": "",
    }
    if not ready_for_cadence_watch:
        return payload
    if not watch_window["active"]:
        payload.update(
            {
                "status": "outside_watch_window",
                "detail": (
                    "paper-live cadence watch is outside the Atlanta/ET session window "
                    f"({watch_window['start']}-{watch_window['end']})"
                ),
            }
        )
        return payload
    if today_count >= min_daily_closes:
        payload.update(
            {
                "status": "cadence_ok",
                "detail": f"{today_count} paper close event(s) recorded today",
            }
        )
        return payload

    payload.update(
        {
            "status": "idle_warning",
            "passed": False,
            "detail": (
                f"{today_count} paper close events today during the Atlanta/ET watch window; "
                "paper-live is ready, so inspect supervisor heartbeat, signal cadence, feed health, "
                "and broker/router rejects before the session drifts."
            ),
            "next_action": (
                "Inspect supervisor heartbeat and daily close cadence: "
                "python -m eta_engine.scripts.supervisor_heartbeat_check --json; "
                "python -m eta_engine.scripts.bot_scoreboard --since-days 1 --include-close-only --json"
            ),
        }
    )
    return payload


def build_transition_check(
    *,
    check_client_portal: bool = False,
    max_watchdog_age_s: int = 180,
    limit: int = 5,
    now: datetime | None = None,
    trade_closes_path: Path = _TRADE_CLOSES_PATH,
) -> dict[str, Any]:
    """Return a read-only paper-live transition verdict."""
    now = now or datetime.now(UTC)
    ibkr_status = ibkr_surface_status.build_status(
        check_client_portal=check_client_portal,
    )
    release_guard = ibgateway_release_guard.run_guard(
        execute=False,
        max_watchdog_age_s=max_watchdog_age_s,
    )
    queue = operator_queue_snapshot.build_snapshot(limit=limit, refresh_readiness=True)
    order_api_status = _latest_order_api_status()

    paper_live_ready = bool(ibkr_status.get("summary", {}).get("paper_live_ready"))
    release_ready = (
        str(release_guard.get("status") or "")
        in {
            "ready_to_release",
            "released",
            "already_released",
        }
        and release_guard.get("operator_action_required") is False
    )
    first_op = _first_op_id(queue)
    first_launch_op = _first_launch_op_id(queue)
    op19_detail = _first_launch_detail(queue)
    paper_ready = _paper_ready_count(queue)
    blockers = _blocked_count(queue)
    launch_blockers = _launch_blocked_count(queue)
    non_authoritative_host = _non_authoritative_gateway_host(queue, release_guard)
    stale_op19_cleared = first_launch_op == "OP-19" and paper_live_ready and release_ready
    effective_launch_blockers = max(launch_blockers - 1, 0) if stale_op19_cleared else launch_blockers
    op19_clear = first_launch_op != "OP-19" or stale_op19_cleared
    op19_next_action = _op19_next_action(
        queue,
        release_guard,
        non_authoritative_gateway_host=non_authoritative_host,
    )

    gates = [
        _gate(
            "tws_api_4002",
            passed=paper_live_ready,
            detail=str(ibkr_status.get("summary", {}).get("operator_action") or ""),
            next_action=(
                _VPS_TWS_WATCHDOG_ACTION
                if non_authoritative_host
                else "python -m eta_engine.scripts.tws_watchdog --host 127.0.0.1 --port 4002"
            ),
        ),
        _gate(
            "ibgateway_release_guard",
            passed=release_ready,
            detail=str(release_guard.get("reason") or release_guard.get("status") or ""),
            next_action=(
                _VPS_RELEASE_GUARD_ACTION
                if non_authoritative_host
                else "python -m eta_engine.scripts.ibgateway_release_guard"
            ),
        ),
        _gate(
            "op19_gateway_runtime",
            passed=op19_clear,
            detail=(
                "OP-19 is clear"
                if first_launch_op != "OP-19"
                else "OP-19 stale: IB Gateway/API 4002 and the release guard are healthy"
                if stale_op19_cleared
                else (op19_detail or "OP-19 is still the top blocker: IB Gateway 10.46/API 4002 is not recovered")
            ),
            next_action="" if stale_op19_cleared else op19_next_action,
        ),
        _gate(
            "paper_ready_bots",
            passed=paper_ready > 0,
            detail=f"{paper_ready} bot(s) are paper-ready in the strategy readiness snapshot",
            next_action="python -m eta_engine.scripts.paper_live_launch_check --json",
        ),
    ]
    if isinstance(order_api_status, dict) and order_api_status.get("status") == "read_only":
        gates.append(
            _gate(
                "ibkr_order_api",
                passed=False,
                detail=str(
                    order_api_status.get("detail")
                    or "IB Gateway API is in Read-Only mode; paper_live order entry would be rejected."
                ),
                next_action=str(
                    order_api_status.get("operator_action")
                    or "Uncheck Read-Only API in IB Gateway API settings, then rerun the verifier."
                ),
            ),
        )
    base_critical_ready = all(gate["passed"] for gate in gates if gate["critical"])
    ready_for_cadence_watch = base_critical_ready and effective_launch_blockers == 0
    cadence = _paper_live_cadence_payload(
        ready_for_cadence_watch=ready_for_cadence_watch,
        now=now,
        trade_closes_path=trade_closes_path,
    )
    gates.append(
        _gate(
            "paper_live_close_cadence",
            passed=bool(cadence.get("passed")),
            critical=False,
            detail=str(cadence.get("detail") or ""),
            next_action=str(cadence.get("next_action") or ""),
        )
    )
    critical_ready = all(gate["passed"] for gate in gates if gate["critical"])
    status = "ready_to_launch_paper_live" if critical_ready and effective_launch_blockers == 0 else "blocked"

    return {
        "schema_version": 1,
        "generated_at": _utc_now_iso(),
        "status": status,
        "critical_ready": critical_ready,
        "launch_command": _LAUNCH_COMMAND if critical_ready and effective_launch_blockers == 0 else "",
        "operator_queue_blocked_count": blockers,
        "operator_queue_first_blocker_op_id": first_op or None,
        "operator_queue_first_next_action": queue.get("first_next_action"),
        "operator_queue_launch_blocked_count": launch_blockers,
        "operator_queue_effective_launch_blocked_count": effective_launch_blockers,
        "operator_queue_stale_op19_cleared": stale_op19_cleared,
        "operator_queue_warning_blocked_count": max(blockers - launch_blockers, 0),
        "operator_queue_first_launch_blocker_op_id": (first_launch_op or None) if not stale_op19_cleared else None,
        "operator_queue_first_launch_next_action": (
            None
            if effective_launch_blockers == 0
            else op19_next_action or queue.get("first_launch_next_action")
        ),
        "non_authoritative_gateway_host": non_authoritative_host,
        "paper_ready_bots": paper_ready,
        "paper_live_cadence": cadence,
        "gates": gates,
        "ibkr_surface_status": ibkr_status,
        "release_guard": release_guard,
        "ibkr_order_api_status": order_api_status,
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
    parser.add_argument("--trade-closes-path", type=Path, default=_TRADE_CLOSES_PATH)
    parser.add_argument("--no-write", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.no_write:
        try:
            args.out = workspace_roots.resolve_under_workspace(args.out, label="--out")
        except ValueError as exc:
            parser.error(str(exc))
    payload = build_transition_check(
        check_client_portal=args.check_client_portal,
        max_watchdog_age_s=args.max_watchdog_age_s,
        limit=max(1, args.limit),
        trade_closes_path=args.trade_closes_path,
    )
    if not args.no_write:
        write_transition_check(payload, args.out)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if payload["status"] == "ready_to_launch_paper_live" else 1


if __name__ == "__main__":
    raise SystemExit(main())
