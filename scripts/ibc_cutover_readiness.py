"""Read-only IBC cutover readiness probe.

This script answers a narrow operator question:
is the VPS ready to switch the healthy direct IBKR gateway lane over to
IBC-managed launch without breaking paper_live?

It never mutates tasks, never starts Gateway, and never writes credentials.
It only inspects the currently staged IBC runtime, credential-source
availability, and the direct paper-live truth surfaces.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.scripts import workspace_roots

try:
    import winreg  # type: ignore
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None  # type: ignore[assignment]

_DEFAULT_OUT = workspace_roots.ETA_IBC_CUTOVER_READINESS_PATH
_ETA_ENV_PATH = workspace_roots.ETA_ENGINE_ROOT / ".env"
_IBKR_JSON_PATH = workspace_roots.ETA_ENGINE_ROOT / "secrets" / "ibkr_credentials.json"
_IBC_PRIVATE_CONFIG_PATH = workspace_roots.WORKSPACE_ROOT / "var" / "eta_engine" / "ibc" / "private" / "config.ini"
_IBC_PASSWORD_FILES = (
    workspace_roots.WORKSPACE_ROOT / "var" / "eta_engine" / "ibc" / "private" / "password.txt",
    workspace_roots.WORKSPACE_ROOT / "var" / "eta_engine" / "ibc" / "private" / "ibkr_password.txt",
    workspace_roots.ETA_ENGINE_ROOT / "secrets" / "ibkr_password.txt",
)
_IBC_INSTALL_STATE_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "ibc_install.json"
_IBG_REPAIR_STATE_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "ibgateway_repair.json"
_PAPER_LIVE_STATE_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "paper_live_transition_check.json"
_TWS_WATCHDOG_STATE_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "tws_watchdog.json"
_PLACEHOLDER_MARKERS = (
    "replace",
    "placeholder",
    "todo",
    "changeme",
    "change me",
    "set me",
    "real_ibkr_password",
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_key_value_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    except OSError:
        return {}
    return result


def _is_secret_sentinel(value: object) -> bool:
    if not isinstance(value, str):
        return False
    token = value.strip()
    if not token:
        return True
    upper = token.upper()
    return any(marker.upper() in upper for marker in _PLACEHOLDER_MARKERS) or (
        token.startswith("<") and token.endswith(">") and "PASSWORD" in upper
    )


def _usable_secret(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not _is_secret_sentinel(value)


def _read_registry_env(name: str, hive: object, subkey: str) -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(hive, subkey):  # type: ignore[arg-type]
            value, _ = winreg.QueryValueEx(winreg.OpenKey(hive, subkey), name)  # type: ignore[arg-type]
    except OSError:
        return False
    return bool(str(value).strip())


def _current_env_present(name: str) -> bool:
    return bool(str(os.environ.get(name, "")).strip())


def _registry_sources(name: str) -> list[str]:
    sources: list[str] = []
    if _current_env_present(name):
        sources.append(f"process_env:{name}")
    if winreg is not None:
        if _read_registry_env(
            name,
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ):
            sources.append(f"machine_env:{name}")
        if _read_registry_env(name, winreg.HKEY_CURRENT_USER, r"Environment"):
            sources.append(f"user_env:{name}")
    return sources


def _json_login_present(payload: dict[str, Any]) -> bool:
    return any(_usable_secret(payload.get(key)) for key in ("username", "user", "login", "ib_login_id", "user_id"))


def _json_password_present(payload: dict[str, Any]) -> bool:
    return any(_usable_secret(payload.get(key)) for key in ("password", "pass", "ib_password"))


def _collect_credential_sources() -> dict[str, Any]:
    dot_env = _read_key_value_map(_ETA_ENV_PATH)
    json_creds = _read_json(_IBKR_JSON_PATH)
    ibc_private = _read_key_value_map(_IBC_PRIVATE_CONFIG_PATH)

    login_sources = _registry_sources("ETA_IBC_LOGIN_ID")
    login_sources.extend(_registry_sources("IBKR_USERNAME"))
    login_sources.extend(_registry_sources("IBKR_LOGIN_ID"))
    if _usable_secret(dot_env.get("ETA_IBC_LOGIN_ID")):
        login_sources.append("eta_env:ETA_IBC_LOGIN_ID")
    if _usable_secret(dot_env.get("IBKR_USERNAME")):
        login_sources.append("eta_env:IBKR_USERNAME")
    if _usable_secret(dot_env.get("IBKR_LOGIN_ID")):
        login_sources.append("eta_env:IBKR_LOGIN_ID")
    if _json_login_present(json_creds):
        login_sources.append("json:ibkr_credentials")
    if _usable_secret(ibc_private.get("IbLoginId")):
        login_sources.append("ibc_private_config:IbLoginId")

    password_sources = _registry_sources("ETA_IBC_PASSWORD")
    password_sources.extend(_registry_sources("IBKR_PASSWORD"))
    if _usable_secret(dot_env.get("ETA_IBC_PASSWORD")):
        password_sources.append("eta_env:ETA_IBC_PASSWORD")
    if _usable_secret(dot_env.get("IBKR_PASSWORD")):
        password_sources.append("eta_env:IBKR_PASSWORD")
    if _json_password_present(json_creds):
        password_sources.append("json:ibkr_credentials")
    if _usable_secret(ibc_private.get("IbPassword")):
        password_sources.append("ibc_private_config:IbPassword")
    for path in _IBC_PASSWORD_FILES:
        try:
            line = path.read_text(encoding="utf-8").splitlines()[0].strip() if path.exists() else ""
        except (OSError, IndexError):
            line = ""
        if _usable_secret(line):
            password_sources.append(f"password_file:{path}")

    return {
        "login_present": bool(login_sources),
        "password_present": bool(password_sources),
        "login_sources": login_sources,
        "password_sources": password_sources,
        "eta_env_path": str(_ETA_ENV_PATH),
        "ibkr_json_path": str(_IBKR_JSON_PATH),
        "ibc_private_config_path": str(_IBC_PRIVATE_CONFIG_PATH),
        "ibc_private_config_exists": _IBC_PRIVATE_CONFIG_PATH.exists(),
    }


def _launcher_mode(repair_state: dict[str, Any]) -> str:
    raw_mode = str(repair_state.get("launcher_mode") or "").strip().lower()
    single_source = repair_state.get("single_source")
    payload = single_source if isinstance(single_source, dict) else {}
    actions = payload.get("task_actions")
    action_payload = actions if isinstance(actions, dict) else {}
    relevant_actions = [
        str(action_payload.get(name) or "")
        for name in ("ETA-IBGateway", "ETA-IBGateway-RunNow", "ETA-IBGateway-DailyRestart")
    ]
    if any("-UseIbc" in action for action in relevant_actions):
        return "ibc"
    if any("start_ibgateway.ps1" in action for action in relevant_actions):
        return "direct"
    return raw_mode or "unknown"


def build_readiness() -> dict[str, Any]:
    install_state = _read_json(_IBC_INSTALL_STATE_PATH)
    repair_state = _read_json(_IBG_REPAIR_STATE_PATH)
    transition_state = _read_json(_PAPER_LIVE_STATE_PATH)
    tws_watchdog = _read_json(_TWS_WATCHDOG_STATE_PATH)
    credentials = _collect_credential_sources()

    installed = install_state.get("installed") is True
    direct_lane_ready = (
        transition_state.get("critical_ready") is True
        and str(transition_state.get("status") or "") == "ready_to_launch_paper_live"
    )
    tws_healthy = tws_watchdog.get("healthy") is True
    launcher_mode = _launcher_mode(repair_state)
    unattended_ready = credentials["login_present"] and credentials["password_present"]

    if not installed:
        status = "ibc_runtime_missing"
        operator_action_required = True
        operator_action = (
            "Install IBC first: powershell -ExecutionPolicy Bypass -File "
            "C:\\EvolutionaryTradingAlgo\\eta_engine\\deploy\\scripts\\install_ibc.ps1 -Install"
        )
    elif not unattended_ready:
        status = "staged_waiting_for_credentials"
        operator_action_required = True
        operator_action = (
            "Seed the missing IBC password source, then run: powershell -ExecutionPolicy Bypass -File "
            "C:\\EvolutionaryTradingAlgo\\eta_engine\\deploy\\scripts\\set_ibc_credentials.ps1 -PromptForPassword"
        )
    elif not tws_healthy or not direct_lane_ready:
        status = "direct_lane_not_ready_for_cutover"
        operator_action_required = True
        operator_action = (
            "Keep IBC cutover paused until the direct paper_live lane is healthy. "
            "Refresh tws_watchdog and paper_live_transition_check first."
        )
    elif unattended_ready and launcher_mode == "ibc":
        status = "ibc_cutover_active"
        operator_action_required = False
        operator_action = ""
    elif unattended_ready:
        status = "ready_for_ibc_cutover"
        operator_action_required = False
        operator_action = (
            "Run the staged cutover: powershell -ExecutionPolicy Bypass -File "
            "C:\\EvolutionaryTradingAlgo\\eta_engine\\deploy\\scripts\\repair_ibgateway_vps.ps1 "
            "-RepairTasks -EnforceSingleSource -UseIbc"
        )
    return {
        "schema_version": 1,
        "generated_at": _utc_now_iso(),
        "status": status,
        "operator_action_required": operator_action_required,
        "operator_action": operator_action,
        "direct_lane_ready": direct_lane_ready,
        "tws_watchdog_healthy": tws_healthy,
        "launcher_mode": launcher_mode,
        "unattended_credential_ready": unattended_ready,
        "ibc_install_state": {
            "installed": installed,
            "install_dir": install_state.get("install_dir"),
            "current_install_dir": install_state.get("current_install_dir"),
            "start_ibc_path": install_state.get("start_ibc_path"),
            "config_template_path": install_state.get("config_template_path"),
        },
        "credentials": credentials,
        "paper_live_transition": {
            "status": transition_state.get("status"),
            "critical_ready": transition_state.get("critical_ready"),
            "operator_queue_launch_blocked_count": transition_state.get("operator_queue_launch_blocked_count"),
            "operator_queue_warning_blocked_count": transition_state.get("operator_queue_warning_blocked_count"),
        },
    }


def write_readiness(payload: dict[str, Any], path: Path = _DEFAULT_OUT) -> Path:
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--strict", action="store_true", help="exit 2 when operator action is still required")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.no_write:
        try:
            args.out = workspace_roots.resolve_under_workspace(args.out, label="--out")
        except ValueError as exc:
            parser.error(str(exc))
    payload = build_readiness()
    if not args.no_write:
        write_readiness(payload, args.out)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 2 if args.strict and payload["operator_action_required"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
