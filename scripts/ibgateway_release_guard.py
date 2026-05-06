"""Guarded IB Gateway paper-live release helper.

This script is the safe handoff after the operator completes the visible
IBKR Gateway login or two-factor prompt. It never bypasses IBKR auth. It only
clears ETA's order-entry hold when the TWS watchdog has fresh, healthy socket
and API-handshake evidence, then restarts the paper-live router/supervisor
tasks that are allowed to submit orders.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.scripts import workspace_roots
from eta_engine.scripts.runtime_order_hold import load_order_entry_hold, write_order_entry_hold

logger = logging.getLogger(__name__)

type JsonValue = str | int | float | bool | None | dict[str, JsonValue] | list[JsonValue]
type JsonDict = dict[str, JsonValue]

_DEFAULT_TWS_STATUS_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "tws_watchdog.json"
_DEFAULT_HOLD_PATH = workspace_roots.ETA_ORDER_ENTRY_HOLD_PATH
_DEFAULT_REAUTH_STATE_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "ibgateway_reauth.json"
_DEFAULT_MAX_WATCHDOG_AGE_S = 180
_CLEAR_REASON = "ibgateway_manual_login_verified_healthy"
_IBGATEWAY_HOLD_PREFIX = "ibgateway_"
_RELEASE_TASKS: tuple[tuple[str, str], ...] = (
    ("Enable-ScheduledTask", "ETA-IBGateway-Reauth"),
    ("Start-ScheduledTask", "ETA-Broker-Router"),
    ("Start-ScheduledTask", "ETA-Jarvis-Strategy-Supervisor"),
)


def _read_json(path: Path) -> JsonDict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_json(path: Path, data: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat()


def _task_command(verb: str, task_name: str) -> str:
    escaped = task_name.replace("'", "''")
    return f"{verb} -TaskName '{escaped}' -ErrorAction SilentlyContinue"


def _run_task_command(verb: str, task_name: str) -> int:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _task_command(verb, task_name),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        logger.warning("%s failed for %s: %s", verb, task_name, completed.stderr.strip())
    return completed.returncode


def _base_state(
    *,
    status: str,
    action: str,
    now: datetime,
    reason: str,
    operator_action_required: bool,
    operator_action: str = "",
) -> JsonDict:
    return {
        "generated_at_utc": _iso(now),
        "status": status,
        "action": action,
        "reason": reason,
        "operator_action_required": operator_action_required,
        "operator_action": operator_action,
    }


def _watchdog_age_s(tws_status: JsonDict, now: datetime) -> int | None:
    checked_at = _parse_time(tws_status.get("checked_at"))
    if checked_at is None:
        return None
    return max(0, int((now.astimezone(UTC) - checked_at).total_seconds()))


def _is_fresh_healthy_watchdog(
    tws_status: JsonDict,
    *,
    now: datetime,
    max_watchdog_age_s: int,
) -> tuple[bool, str, int | None]:
    if not tws_status:
        return False, "missing tws_watchdog.json", None
    age_s = _watchdog_age_s(tws_status, now)
    if age_s is None:
        return False, "watchdog status lacks checked_at", None
    if age_s > max_watchdog_age_s:
        return False, f"watchdog status is stale ({age_s}s old; max {max_watchdog_age_s}s)", age_s
    details = tws_status.get("details") if isinstance(tws_status.get("details"), dict) else {}
    if not bool(tws_status.get("healthy")):
        return False, "watchdog is unhealthy", age_s
    if details and not (bool(details.get("socket_ok")) and bool(details.get("handshake_ok"))):
        return False, "watchdog is healthy but socket/handshake details are not both true", age_s
    return True, "watchdog is fresh and healthy", age_s


def _hold_can_be_cleared(hold_reason: str) -> bool:
    return not hold_reason or hold_reason.startswith(_IBGATEWAY_HOLD_PREFIX)


def run_guard(
    *,
    tws_status_path: Path = _DEFAULT_TWS_STATUS_PATH,
    hold_path: Path = _DEFAULT_HOLD_PATH,
    reauth_state_path: Path = _DEFAULT_REAUTH_STATE_PATH,
    execute: bool = False,
    now: datetime | None = None,
    max_watchdog_age_s: int = _DEFAULT_MAX_WATCHDOG_AGE_S,
    release_tasks: tuple[tuple[str, str], ...] = _RELEASE_TASKS,
) -> JsonDict:
    """Validate IBKR Gateway health and optionally release paper-live order entry."""
    effective_now = now or datetime.now(UTC)
    tws_status = _read_json(Path(tws_status_path))
    ok, reason, age_s = _is_fresh_healthy_watchdog(
        tws_status,
        now=effective_now,
        max_watchdog_age_s=max_watchdog_age_s,
    )
    hold = load_order_entry_hold(Path(hold_path))
    hold_reason = hold.reason
    if not ok:
        state = _base_state(
            status="blocked_watchdog_unhealthy",
            action="none",
            now=effective_now,
            reason=reason,
            operator_action_required=True,
            operator_action="Complete IBKR Gateway login/2FA, then run tws_watchdog until it is freshly healthy.",
        )
        state["watchdog_age_s"] = age_s
        state["hold"] = hold.to_dict()
        return state

    if hold.active and not _hold_can_be_cleared(hold_reason):
        state = _base_state(
            status="blocked_operator_hold",
            action="none",
            now=effective_now,
            reason=f"active non-IBGateway hold remains in force: {hold_reason}",
            operator_action_required=True,
            operator_action="Do not clear unrelated operator holds with the IB Gateway release guard.",
        )
        state["watchdog_age_s"] = age_s
        state["hold"] = hold.to_dict()
        return state

    task_results: list[JsonDict] = []
    if execute:
        write_order_entry_hold(active=False, reason=_CLEAR_REASON, path=Path(hold_path))
        for verb, task_name in release_tasks:
            returncode = _run_task_command(verb, task_name)
            task_results.append({"verb": verb, "task_name": task_name, "returncode": returncode})
    else:
        task_results = [
            {"verb": verb, "task_name": task_name, "returncode": None}
            for verb, task_name in release_tasks
        ]

    status = "released" if execute else "ready_to_release"
    state = _base_state(
        status=status,
        action="release" if execute else "dry_run",
        now=effective_now,
        reason=_CLEAR_REASON if execute else "fresh healthy watchdog; rerun with --execute to clear hold",
        operator_action_required=False,
    )
    state.update(
        {
            "healthy": True,
            "watchdog_age_s": age_s,
            "tws_status_path": str(tws_status_path),
            "hold_path": str(hold_path),
            "task_results": task_results,
        },
    )
    if execute:
        _write_json(
            Path(reauth_state_path),
            {
                "generated_at_utc": _iso(effective_now),
                "status": "healthy_released",
                "action": "release",
                "healthy": True,
                "operator_action_required": False,
                "operator_action": "",
                "reason": "IB Gateway watchdog was freshly healthy; paper-live release guard cleared ETA order hold.",
                "task_results": task_results,
                "tws_status_path": str(tws_status_path),
            },
        )
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Clear the IBKR hold and start release tasks.")
    parser.add_argument("--tws-status", type=Path, default=_DEFAULT_TWS_STATUS_PATH)
    parser.add_argument("--hold-path", type=Path, default=_DEFAULT_HOLD_PATH)
    parser.add_argument("--reauth-state", type=Path, default=_DEFAULT_REAUTH_STATE_PATH)
    parser.add_argument("--max-watchdog-age-s", type=int, default=_DEFAULT_MAX_WATCHDOG_AGE_S)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)
    result = run_guard(
        tws_status_path=args.tws_status,
        hold_path=args.hold_path,
        reauth_state_path=args.reauth_state,
        execute=args.execute,
        max_watchdog_age_s=args.max_watchdog_age_s,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("operator_action_required") is False else 1


if __name__ == "__main__":
    raise SystemExit(main())
