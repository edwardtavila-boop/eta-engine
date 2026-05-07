"""Safe IB Gateway auto-recovery controller.

This controller is intentionally scoped to the TWS/IB Gateway lane. It does
not automate raw credentials or bypass IBKR two-factor prompts. Instead, it
uses the canonical Windows scheduled tasks that already own the Gateway
profile, applies restart cooldowns, and writes a clear state file when manual
IBKR login/2FA is required.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eta_engine.scripts import workspace_roots

logger = logging.getLogger(__name__)

type JsonValue = str | int | float | bool | None | dict[str, JsonValue] | list[JsonValue]
type JsonDict = dict[str, JsonValue]

REAUTH_TASK_NAME = "ETA-IBGateway-Reauth"
GATEWAY_TASK_NAME = "ETA-IBGateway"
RUN_NOW_TASK_NAME = "ETA-IBGateway-RunNow"
RESTART_TASK_NAME = "ETA-IBGateway-DailyRestart"
DEFAULT_TWS_STATUS_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "tws_watchdog.json"
DEFAULT_REAUTH_STATE_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "ibgateway_reauth.json"
_DEFAULT_TWS_STATUS_PATH = DEFAULT_TWS_STATUS_PATH
_DEFAULT_STATE_PATH = DEFAULT_REAUTH_STATE_PATH
_DEFAULT_GATEWAY_TASK = GATEWAY_TASK_NAME
_DEFAULT_RUN_NOW_TASK = RUN_NOW_TASK_NAME
_DEFAULT_RESTART_TASK = RESTART_TASK_NAME
_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_COOLDOWN_MINUTES = 20
_DEFAULT_MAX_RESTART_ATTEMPTS = 3
_DEFAULT_REPAIR_COMMAND = (
    r"deploy\scripts\repair_ibgateway_vps.ps1 -RepairTasks "
    r"-ApplyJtsIni -ApplyVmOptions -EnforceSingleSource"
)


def _json_dict(value: object) -> JsonDict:
    return value if isinstance(value, dict) else {}


def _read_json(path: Path) -> JsonDict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _json_dict(loaded)


def _write_json(path: Path, data: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _int_value(value: object, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _bool_value(value: object) -> bool:
    return bool(value)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat()


def recovery_lane_metadata(
    *,
    tws_status_path: Path = DEFAULT_TWS_STATUS_PATH,
    state_path: Path = DEFAULT_REAUTH_STATE_PATH,
    gateway_task: str = GATEWAY_TASK_NAME,
    run_now_task: str = RUN_NOW_TASK_NAME,
    restart_task: str = RESTART_TASK_NAME,
) -> JsonDict:
    return {
        "controller_task": REAUTH_TASK_NAME,
        "watchdog_status_path": str(tws_status_path),
        "state_path": str(state_path),
        "gateway_task": gateway_task,
        "run_now_task": run_now_task,
        "restart_task": restart_task,
        "repair_command": _DEFAULT_REPAIR_COMMAND,
    }


def _base_decision(
    *,
    status: str,
    action: str,
    now: datetime,
    tws_status: JsonDict,
    prior_state: JsonDict,
    operator_action_required: bool = False,
    operator_action: str = "",
    reason: str = "",
) -> JsonDict:
    return {
        "generated_at_utc": _iso(now),
        "status": status,
        "action": action,
        "reason": reason,
        "healthy": _bool_value(tws_status.get("healthy")),
        "consecutive_failures": _int_value(tws_status.get("consecutive_failures"), 0),
        "restart_attempts": _int_value(prior_state.get("restart_attempts"), 0),
        "last_restart_at": str(prior_state.get("last_restart_at") or ""),
        "operator_action_required": operator_action_required,
        "operator_action": operator_action,
    }


def decide_reauth_action(
    tws_status: JsonDict,
    prior_state: JsonDict,
    *,
    now: datetime | None = None,
    failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
    cooldown_minutes: int = _DEFAULT_COOLDOWN_MINUTES,
    max_restart_attempts: int = _DEFAULT_MAX_RESTART_ATTEMPTS,
) -> JsonDict:
    """Choose the next safe Gateway recovery action from watchdog state."""
    effective_now = now or datetime.now(UTC)
    if not tws_status:
        return _base_decision(
            status="missing_watchdog_status",
            action="none",
            now=effective_now,
            tws_status=tws_status,
            prior_state=prior_state,
            operator_action_required=True,
            operator_action="Run the TWS watchdog first; ibgateway_reauth needs fresh health evidence.",
            reason="missing tws_watchdog.json",
        )

    if _bool_value(tws_status.get("healthy")):
        decision = _base_decision(
            status="healthy",
            action="none",
            now=effective_now,
            tws_status=tws_status,
            prior_state=prior_state,
            reason="IB Gateway API socket and handshake are healthy.",
        )
        decision["restart_attempts"] = 0
        decision["last_restart_at"] = ""
        return decision

    failures = _int_value(tws_status.get("consecutive_failures"), 0)
    if failures < failure_threshold:
        return _base_decision(
            status="waiting_for_failures",
            action="none",
            now=effective_now,
            tws_status=tws_status,
            prior_state=prior_state,
            reason=f"Waiting for {failure_threshold} consecutive failures before recovery action.",
        )

    details = _json_dict(tws_status.get("details"))
    gateway_process = _json_dict(details.get("gateway_process"))
    process_running = _bool_value(gateway_process.get("running"))
    if not process_running:
        return _base_decision(
            status="started_gateway",
            action="start_gateway",
            now=effective_now,
            tws_status=tws_status,
            prior_state=prior_state,
            reason="IB Gateway process is not running; start the canonical run-now task.",
        )

    restart_attempts = _int_value(prior_state.get("restart_attempts"), 0)
    last_restart_at = _parse_time(prior_state.get("last_restart_at"))
    cooldown = timedelta(minutes=cooldown_minutes)
    if last_restart_at is not None and effective_now - last_restart_at < cooldown:
        decision = _base_decision(
            status="auth_pending",
            action="none",
            now=effective_now,
            tws_status=tws_status,
            prior_state=prior_state,
            operator_action_required=True,
            operator_action=(
                "IB Gateway is running but the API is still closed; complete the IBKR Gateway login or "
                "two-factor prompt, then let the watchdog clear."
            ),
            reason="A restart was already requested inside the cooldown window.",
        )
        decision["restart_attempts"] = restart_attempts
        decision["last_restart_at"] = _iso(last_restart_at)
        return decision

    if restart_attempts >= max_restart_attempts:
        return _base_decision(
            status="max_restarts_exceeded",
            action="none",
            now=effective_now,
            tws_status=tws_status,
            prior_state=prior_state,
            operator_action_required=True,
            operator_action=(
                "Auto-restart attempts are capped; perform manual IB Gateway recovery and verify login/2FA."
            ),
            reason=f"Reached max_restart_attempts={max_restart_attempts}.",
        )

    return _base_decision(
        status="restart_requested",
        action="restart_gateway",
        now=effective_now,
        tws_status=tws_status,
        prior_state=prior_state,
        reason="IB Gateway is running but API/auth health is dead; restart the canonical Gateway task.",
    )


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


def _scheduled_task_exists(task_name: str) -> bool:
    """Return whether a Windows scheduled task exists.

    Non-Windows test/CI hosts may not have PowerShell or Task Scheduler; in
    that case return True so portable unit tests can exercise decision logic
    without requiring the Windows service layer. On the VPS this returns False
    for missing recovery tasks, which prevents a misleading "started_gateway"
    state when there is no task to start.
    """
    escaped = task_name.replace("'", "''")
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                (
                    "$task = Get-ScheduledTask "
                    f"-TaskName '{escaped}' -ErrorAction SilentlyContinue; "
                    "if ($null -eq $task) { exit 44 }; exit 0"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Unable to query scheduled task %s: %s", task_name, exc)
        return True
    return completed.returncode == 0


def _start_scheduled_task(task_name: str) -> int:
    return _run_task_command("Start-ScheduledTask", task_name)


def _stop_scheduled_task(task_name: str) -> int:
    return _run_task_command("Stop-ScheduledTask", task_name)


def _restart_scheduled_task(task_name: str) -> int:
    _stop_scheduled_task(task_name)
    time.sleep(5)
    return _start_scheduled_task(task_name)


def _state_for_command_result(
    decision: JsonDict,
    *,
    now: datetime,
    action: str,
    task_name: str,
    returncode: int,
) -> JsonDict:
    updated = dict(decision)
    updated["last_task_name"] = task_name
    updated["last_task_returncode"] = returncode
    if returncode != 0:
        updated["status"] = "task_start_failed"
        updated["operator_action_required"] = True
        updated["operator_action"] = f"Scheduled task {task_name} failed to start; inspect Task Scheduler history."
        return updated

    if action == "restart_gateway":
        updated["restart_attempts"] = _int_value(decision.get("restart_attempts"), 0) + 1
        updated["last_restart_at"] = _iso(now)
    elif action == "start_gateway":
        updated["last_start_at"] = _iso(now)
    return updated


def run_controller(
    *,
    tws_status_path: Path = _DEFAULT_TWS_STATUS_PATH,
    state_path: Path = _DEFAULT_STATE_PATH,
    gateway_task: str = _DEFAULT_GATEWAY_TASK,
    run_now_task: str = _DEFAULT_RUN_NOW_TASK,
    restart_task: str = _DEFAULT_RESTART_TASK,
    execute: bool = False,
    now: datetime | None = None,
    failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
    cooldown_minutes: int = _DEFAULT_COOLDOWN_MINUTES,
    max_restart_attempts: int = _DEFAULT_MAX_RESTART_ATTEMPTS,
) -> JsonDict:
    """Run one controller tick and persist the operator-facing state."""
    effective_now = now or datetime.now(UTC)
    lane = recovery_lane_metadata(
        tws_status_path=tws_status_path,
        state_path=state_path,
        gateway_task=gateway_task,
        run_now_task=run_now_task,
        restart_task=restart_task,
    )
    tws_status = _read_json(tws_status_path)
    prior_state = _read_json(state_path)
    decision = decide_reauth_action(
        tws_status,
        prior_state,
        now=effective_now,
        failure_threshold=failure_threshold,
        cooldown_minutes=cooldown_minutes,
        max_restart_attempts=max_restart_attempts,
    )

    task_name = ""
    action = str(decision.get("action") or "")
    if action == "start_gateway":
        if _scheduled_task_exists(run_now_task):
            task_name = run_now_task
        elif gateway_task and _scheduled_task_exists(gateway_task):
            task_name = gateway_task
            decision["reason"] = (
                f"{decision['reason']} Falling back to {gateway_task} because {run_now_task} is missing."
            )
            lane["start_task_mode"] = "gateway_task_fallback"
        else:
            task_name = run_now_task
    elif action == "restart_gateway":
        task_name = restart_task

    state = dict(decision)
    state["tws_status_path"] = str(tws_status_path)
    state["recovery_lane"] = lane
    state["execute"] = execute
    if task_name:
        state["last_task_name"] = task_name
        state["recovery_lane"]["next_task"] = task_name
        if not _scheduled_task_exists(task_name):
            state.update(
                {
                    "status": "missing_recovery_task",
                    "action": "none",
                    "reason": (
                        f"Required scheduled task {task_name} is missing; "
                        "IB Gateway cannot be started by the reauth controller."
                    ),
                    "operator_action_required": True,
                    "operator_action": (
                        "Install/configure canonical IB Gateway 10.46, then run "
                        f"{lane['repair_command']}."
                    ),
                },
            )
            _write_json(state_path, state)
            return state
    if execute and task_name:
        if action == "restart_gateway":
            returncode = _restart_scheduled_task(task_name)
        else:
            returncode = _start_scheduled_task(task_name)
        state = _state_for_command_result(
            state,
            now=effective_now,
            action=action,
            task_name=task_name,
            returncode=returncode,
        )

    _write_json(state_path, state)
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Actually start Gateway scheduled tasks when needed.")
    parser.add_argument("--tws-status", type=Path, default=_DEFAULT_TWS_STATUS_PATH)
    parser.add_argument("--state", type=Path, default=_DEFAULT_STATE_PATH)
    parser.add_argument("--gateway-task", default=_DEFAULT_GATEWAY_TASK)
    parser.add_argument("--run-now-task", default=_DEFAULT_RUN_NOW_TASK)
    parser.add_argument("--restart-task", default=_DEFAULT_RESTART_TASK)
    parser.add_argument("--failure-threshold", type=int, default=_DEFAULT_FAILURE_THRESHOLD)
    parser.add_argument("--cooldown-minutes", type=int, default=_DEFAULT_COOLDOWN_MINUTES)
    parser.add_argument("--max-restart-attempts", type=int, default=_DEFAULT_MAX_RESTART_ATTEMPTS)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)
    state = run_controller(
        tws_status_path=args.tws_status,
        state_path=args.state,
        gateway_task=args.gateway_task,
        run_now_task=args.run_now_task,
        restart_task=args.restart_task,
        execute=args.execute,
        failure_threshold=args.failure_threshold,
        cooldown_minutes=args.cooldown_minutes,
        max_restart_attempts=args.max_restart_attempts,
    )
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
