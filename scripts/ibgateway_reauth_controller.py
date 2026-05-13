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
import os
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
DEFAULT_IBC_PASSWORD_FILE = workspace_roots.ETA_RUNTIME_STATE_DIR / "ibkr_pw.txt"
DEFAULT_IBKR_CREDENTIAL_JSON_PATH = workspace_roots.ETA_ENGINE_ROOT / "secrets" / "ibkr_credentials.json"
DEFAULT_DOTENV_PATH = workspace_roots.ETA_ENGINE_ROOT / ".env"
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


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def _read_first_line(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").splitlines()[0].strip()
    except (IndexError, OSError):
        return ""


def _looks_like_secret_placeholder(value: str) -> bool:
    text = value.strip()
    if not text:
        return True
    upper = text.upper()
    return (
        upper.startswith(("REPLACE", "PLACEHOLDER", "TODO", "CHANGEME"))
        or ("REAL_IBKR_" + "PASSWORD") in upper
        or (text.startswith("<") and "password" in text.lower() and text.endswith(">"))
    )


def _usable_secret(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if _looks_like_secret_placeholder(text) else text


def _read_dotenv(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in result:
            result[key] = value.strip().strip("'").strip('"')
    return result


def _first_present(values: list[object]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_usable_secret(values: list[object]) -> str:
    for value in values:
        text = _usable_secret(value)
        if text:
            return text
    return ""


def ibc_credential_status(
    *,
    password_file: Path = DEFAULT_IBC_PASSWORD_FILE,
    credential_json_path: Path = DEFAULT_IBKR_CREDENTIAL_JSON_PATH,
    dotenv_path: Path = DEFAULT_DOTENV_PATH,
) -> JsonDict:
    dotenv = _read_dotenv(dotenv_path)
    json_creds = _read_json(credential_json_path)
    password_from_file = _read_first_line(password_file)
    user_id = _first_present(
        [
            os.environ.get("ETA_IBC_LOGIN_ID"),
            dotenv.get("ETA_IBC_LOGIN_ID"),
            json_creds.get("username"),
            json_creds.get("user"),
            json_creds.get("login"),
            json_creds.get("ib_login_id"),
            json_creds.get("user_id"),
        ],
    )
    password = _first_usable_secret(
        [
            password_from_file,
            os.environ.get("ETA_IBC_PASSWORD"),
            dotenv.get("ETA_IBC_PASSWORD"),
            json_creds.get("password"),
            json_creds.get("pass"),
            json_creds.get("ib_password"),
        ],
    )
    password_file_present = password_file.exists()
    password_file_placeholder = password_file_present and _looks_like_secret_placeholder(password_from_file)
    return {
        "ready": bool(user_id and password),
        "has_user_id": bool(user_id),
        "has_password": bool(password),
        "password_file": str(password_file),
        "password_file_present": password_file_present,
        "password_file_placeholder": password_file_placeholder,
        "credential_json_path": str(credential_json_path),
        "credential_json_present": credential_json_path.exists(),
        "dotenv_path": str(dotenv_path),
        "dotenv_present": dotenv_path.exists(),
    }


def _json_dict(value: object) -> JsonDict:
    return value if isinstance(value, dict) else {}


def _read_json(path: Path) -> JsonDict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
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
    socket_ok = _bool_value(details.get("socket_ok"))
    gateway_process = _json_dict(details.get("gateway_process"))
    process_running = _bool_value(gateway_process.get("running"))
    if not process_running and not socket_ok:
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


def _scheduled_task_state(task_name: str) -> str:
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
                    "if ($null -eq $task) { exit 44 }; "
                    "Write-Output ([string]$task.State); exit 0"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Unable to query scheduled task state for %s: %s", task_name, exc)
        return "unknown"
    if completed.returncode == 44:
        return "missing"
    if completed.returncode != 0:
        logger.warning("Unable to query scheduled task state for %s: %s", task_name, completed.stderr.strip())
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _scheduled_task_action_text(task_name: str) -> str:
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
                    "if ($null -eq $task) { exit 44 }; "
                    "($task.Actions | ForEach-Object { ($_.Execute + ' ' + $_.Arguments).Trim() }) -join ' || '"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Unable to query scheduled task action for %s: %s", task_name, exc)
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _scheduled_task_uses_ibc(task_name: str) -> bool:
    return "-UseIbc" in _scheduled_task_action_text(task_name)


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


def _scheduled_task_is_runnable(task_name: str) -> bool:
    state = _scheduled_task_state(task_name).lower()
    if state == "disabled":
        return False
    if state in {"missing", ""}:
        return _scheduled_task_exists(task_name)
    return True


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
    check_ibc_credentials: bool = False,
    ibc_password_file: Path = DEFAULT_IBC_PASSWORD_FILE,
    ibc_credential_json_path: Path = DEFAULT_IBKR_CREDENTIAL_JSON_PATH,
    dotenv_path: Path = DEFAULT_DOTENV_PATH,
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
        run_now_exists = _scheduled_task_exists(run_now_task)
        run_now_runnable = run_now_exists and _scheduled_task_is_runnable(run_now_task)
        gateway_runnable = (
            bool(gateway_task)
            and _scheduled_task_exists(gateway_task)
            and _scheduled_task_is_runnable(
                gateway_task,
            )
        )
        if run_now_runnable:
            task_name = run_now_task
        elif gateway_runnable:
            task_name = gateway_task
            decision["reason"] = (
                f"{decision['reason']} Falling back to {gateway_task} because {run_now_task} is missing or disabled."
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
                        f"Install/configure canonical IB Gateway 10.46, then run {lane['repair_command']}."
                    ),
                },
            )
            _write_json(state_path, state)
            return state
        if not _scheduled_task_is_runnable(task_name):
            state.update(
                {
                    "status": "recovery_task_disabled",
                    "action": "none",
                    "reason": f"Required scheduled task {task_name} is disabled.",
                    "operator_action_required": True,
                    "operator_action": (
                        f"Enable scheduled task {task_name}, or repair the canonical IB Gateway task lane with "
                        f"{lane['repair_command']}."
                    ),
                },
            )
            _write_json(state_path, state)
            return state
        if check_ibc_credentials and _scheduled_task_uses_ibc(task_name):
            credential_state = ibc_credential_status(
                password_file=ibc_password_file,
                credential_json_path=ibc_credential_json_path,
                dotenv_path=dotenv_path,
            )
            state["credential_status"] = credential_state
            if not credential_state.get("ready"):
                state.update(
                    {
                        "status": "missing_ibc_credentials",
                        "action": "none",
                        "reason": "IBC recovery task is configured, but usable login/password credentials are missing.",
                        "operator_action_required": True,
                        "operator_action": (
                            "Seed IBC credentials with "
                            r".\eta_engine\deploy\scripts\set_ibc_credentials.ps1 -PromptForPassword, "
                            "then rerun ibgateway_reauth_controller --execute."
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
    parser.add_argument(
        "--skip-ibc-credential-check",
        action="store_true",
        help="Do not preflight IBC login/password presence before starting an IBC scheduled task.",
    )
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
        check_ibc_credentials=not args.skip_ibc_credential_check,
    )
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
