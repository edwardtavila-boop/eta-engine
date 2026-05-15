"""Read-only VPS operations hardening audit.

This is an operator-facing health view, not a trading actuator. It can say
"the VPS/runtime is alive" while still keeping promotion blocked when broker
brackets, paper-soak, or prop gates are not clean yet.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eta_engine.scripts import jarvis_hermes_admin_audit, workspace_roots  # noqa: E402

DEFAULT_OUT = workspace_roots.ETA_VPS_OPS_HARDENING_AUDIT_PATH
ETA_ENGINE_REPO_ROOT = workspace_roots.ETA_ENGINE_ROOT
CURRENT_JARVIS_HERMES_BRIDGE_TASK_COUNT = 8
CRITICAL_SERVICES = ("FmStatusServer",)
LEGACY_COMPAT_SERVICES = (
    "FirmCommandCenter",
    "FirmCommandCenterEdge",
    "FirmCommandCenterTunnel",
    "FirmCore",
    "FirmWatchdog",
    "ETAJarvisSupervisor",
)
OPTIONAL_SERVICES = ("HermesJarvisTelegram",) + LEGACY_COMPAT_SERVICES
REQUIRED_PORTS = (8000, 8421, 8422)
BROKER_PORTS = (4002,)
DASHBOARD_DURABLE_TASKS = (
    "ETA-Dashboard-API",
    "ETA-Proxy-8421",
    "ETA-Dashboard-Proxy-Watchdog",
    "ETA-BrokerStateRefreshHeartbeat",
    "ETA-SupervisorBrokerReconcile",
    "ETA-OperatorQueueHeartbeat",
    "ETA-PaperLiveTransitionCheck",
)
PAPER_LIVE_DURABLE_TASKS = (
    "ETA-PaperLive-Supervisor",
    "ETA-TWS-Watchdog",
    "ETA-IBGateway-Reauth",
)
DATA_PIPELINE_TASKS = (
    "ETA-SymbolIntelCollector",
    "ETA-IndexFutures-Bar-Refresh",
)
IBGATEWAY_TASKS = (
    "ETA-IBGateway",
    "ETA-IBGateway-Autostart",
    "ETA-IBGateway-DailyRestart",
    "ETA-IBGateway-RunNow",
)
WATCHDOG_OBSERVED_TASKS = (
    "ETA-Watchdog",
    "ETA-Watchdog-Restart",
)
CANONICAL_SUPERVISOR_TASK = "ETA-Jarvis-Strategy-Supervisor"
LEGACY_PAPERLIVE_SUPERVISOR_TASK = "ETA-PaperLive-Supervisor"
SUPERVISOR_RECONCILE_MAX_AGE_S = 15 * 60
BROKER_STATE_URL = "http://127.0.0.1:8421/api/live/broker_state"
FUTURES_MONTH_CODES = frozenset("FGHJKMNQUVXZ")
ENDPOINTS = (
    {
        "name": "local_dashboard_api_diagnostics",
        "url": "http://127.0.0.1:8000/api/dashboard/diagnostics",
        "critical": True,
    },
    {
        "name": "local_dashboard_proxy_diagnostics",
        "url": "http://127.0.0.1:8421/api/dashboard/diagnostics",
        "critical": True,
    },
    {
        "name": "local_fm_status",
        "url": "http://127.0.0.1:8422/api/fm/status",
        "critical": True,
    },
    {
        "name": "public_ops_bot_fleet",
        "url": "https://ops.evolutionarytradingalgo.com/api/bot-fleet",
        "critical": False,
    },
)


def _as_dict(value: Any) -> dict[str, Any]:  # noqa: ANN401
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:  # noqa: ANN401
    return value if isinstance(value, list) else []


def _iso_age_seconds(value: object, *, now: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, ((now or datetime.now(UTC)) - parsed).total_seconds())


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"artifact_status": "missing", "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"artifact_status": "unreadable", "path": str(path), "error": str(exc)}
    return payload if isinstance(payload, dict) else {"artifact_status": "invalid", "path": str(path)}


def collect_repo_revision(repo_root: Path = ETA_ENGINE_REPO_ROOT) -> dict[str, Any]:
    """Collect the deployed eta_engine git revision for stale-process checks."""

    def _git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            check=True,
            text=True,
            timeout=3,
        ).stdout.strip()

    payload: dict[str, Any] = {
        "repo_root": str(repo_root),
        "captured_at": datetime.now(UTC).isoformat(),
    }
    try:
        head = _git("rev-parse", "HEAD")
    except Exception as exc:  # noqa: BLE001 - report the gate instead of crashing the audit
        payload.update(
            {
                "status": "error",
                "head": "",
                "head_short": "",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    else:
        payload["status"] = "ok"
        payload["head"] = head
        payload["head_short"] = head[:7]
    with contextlib.suppress(Exception):
        payload["branch"] = _git("branch", "--show-current")
    with contextlib.suppress(Exception):
        payload["dirty"] = bool(_git("status", "--short"))
    return payload


def _run_powershell_json(command: str, *, timeout_s: int = 10) -> Any:  # noqa: ANN401
    try:
        proc = subprocess.run(  # noqa: S603
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)}
    if proc.returncode != 0:
        return {"error": proc.stderr.strip() or proc.stdout.strip(), "returncode": proc.returncode}
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {"error": str(exc), "raw": proc.stdout[:500]}


def collect_service_status() -> dict[str, dict[str, Any]]:
    """Collect Windows service status, returning missing services explicitly."""
    names = list(CRITICAL_SERVICES + OPTIONAL_SERVICES)
    quoted = ", ".join(f"'{name}'" for name in names)
    command = f"""
$names = @({quoted})
$results = foreach ($name in $names) {{
  $service = Get-Service -Name $name -ErrorAction SilentlyContinue
  if ($null -eq $service) {{
    [pscustomobject]@{{Name=$name;Status='Missing';StartType=$null}}
  }} else {{
    [pscustomobject]@{{Name=$service.Name;Status=[string]$service.Status;StartType=[string]$service.StartType}}
  }}
}}
$results | ConvertTo-Json -Depth 4
"""
    payload = _run_powershell_json(command, timeout_s=30)
    if isinstance(payload, dict) and "error" in payload:
        return {name: {"name": name, "status": "Unknown", "error": payload["error"]} for name in names}
    rows = payload if isinstance(payload, list) else [payload]
    services: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = _as_dict(row)
        name = str(item.get("Name") or "")
        if name:
            services[name] = {
                "name": name,
                "status": str(item.get("Status") or "Unknown"),
                "start_type": item.get("StartType"),
            }
    return services


def collect_port_status() -> dict[int, dict[str, Any]]:
    """Collect required listener status for local control-plane ports."""
    quoted = ", ".join(str(port) for port in tuple(REQUIRED_PORTS + BROKER_PORTS))
    command = f"""
$ports = @({quoted})
$results = foreach ($port in $ports) {{
  $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  $owners = @($connections | ForEach-Object {{ $_.OwningProcess }} | Sort-Object -Unique)
  [pscustomobject]@{{Port=$port;Listening=($null -ne $connections);Owners=$owners}}
}}
$results | ConvertTo-Json -Depth 4
"""
    payload = _run_powershell_json(command, timeout_s=30)
    if isinstance(payload, dict) and "error" in payload:
        return {
            port: {"port": port, "listening": False, "error": payload["error"], "owners": []} for port in REQUIRED_PORTS
        }
    rows = payload if isinstance(payload, list) else [payload]
    ports: dict[int, dict[str, Any]] = {}
    for row in rows:
        item = _as_dict(row)
        try:
            port = int(item.get("Port"))
        except (TypeError, ValueError):
            continue
        ports[port] = {
            "port": port,
            "listening": bool(item.get("Listening")),
            "owners": _as_list(item.get("Owners")),
        }
    return ports


def collect_task_status() -> dict[str, dict[str, Any]]:
    """Collect expected scheduled-task status without modifying Task Scheduler."""
    names = list(
        DASHBOARD_DURABLE_TASKS
        + PAPER_LIVE_DURABLE_TASKS
        + DATA_PIPELINE_TASKS
        + IBGATEWAY_TASKS
        + WATCHDOG_OBSERVED_TASKS
        + ("ETA-Autopilot",)
    )
    quoted = ", ".join(f"'{name}'" for name in names)
    command = f"""
$names = @({quoted})
$results = foreach ($name in $names) {{
  $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
  if ($null -eq $task) {{
    [pscustomobject]@{{TaskName=$name;State='Missing';LastTaskResult=$null;LastRunTime=$null;NextRunTime=$null}}
  }} else {{
    $info = Get-ScheduledTaskInfo -TaskName $name -ErrorAction SilentlyContinue
    $actions = (($task.Actions | ForEach-Object {{ "$($_.Execute) $($_.Arguments)" }}) -join " || ")
    [pscustomobject]@{{
      TaskName=$task.TaskName
      State=[string]$task.State
      LastTaskResult=$info.LastTaskResult
      LastRunTime=$info.LastRunTime
      NextRunTime=$info.NextRunTime
      Actions=$actions
    }}
  }}
}}
$results | ConvertTo-Json -Depth 4
"""
    payload = _run_powershell_json(command, timeout_s=30)
    if isinstance(payload, dict) and "error" in payload:
        return {name: {"task_name": name, "state": "Unknown", "error": payload["error"]} for name in names}
    rows = payload if isinstance(payload, list) else [payload]
    tasks: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = _as_dict(row)
        name = str(item.get("TaskName") or "")
        if name:
            tasks[name] = {
                "task_name": name,
                "state": str(item.get("State") or "Unknown"),
                "last_task_result": item.get("LastTaskResult"),
                "last_run_time": item.get("LastRunTime"),
                "next_run_time": item.get("NextRunTime"),
                "actions": str(item.get("Actions") or ""),
            }
    return tasks


def _probe_endpoint(url: str, *, timeout_s: float = 8.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "eta-vps-ops-hardening"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            body = response.read(65_536).decode("utf-8", errors="replace")
            payload: Any
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = body[:500]
            return {
                "ok": 200 <= response.status < 300,
                "status_code": response.status,
                "payload": payload,
            }
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        return {"ok": False, "error": str(exc)}


def collect_endpoint_status() -> dict[str, dict[str, Any]]:
    """Probe local and public read-only health endpoints."""
    return {
        str(endpoint["name"]): {
            **_probe_endpoint(
                str(endpoint["url"]),
                timeout_s=float(endpoint.get("timeout_s", 8.0)),
            ),
            "url": str(endpoint["url"]),
            "critical": bool(endpoint["critical"]),
        }
        for endpoint in ENDPOINTS
    }


def _xml_field(path: Path, field: str) -> str | None:
    if not path.exists():
        return None
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError):
        return None
    value = root.findtext(field)
    return value.strip() if value else None


def collect_service_config_status() -> dict[str, dict[str, Any]]:
    """Compare tracked and installed FmStatusServer WinSW XML."""
    expected = workspace_roots.ETA_ENGINE_ROOT / "deploy" / "FmStatusServer.xml"
    installed = workspace_roots.WORKSPACE_ROOT / "firm_command_center" / "services" / "FmStatusServer.xml"
    expected_executable = _xml_field(expected, "executable")
    installed_executable = _xml_field(installed, "executable")
    expected_arguments = _xml_field(expected, "arguments")
    installed_arguments = _xml_field(installed, "arguments")
    matches = (
        expected.exists()
        and installed.exists()
        and expected_executable == installed_executable
        and expected_arguments == installed_arguments
    )
    return {
        "fm_status_server": {
            "matches_expected": matches,
            "expected_xml": str(expected),
            "installed_xml": str(installed),
            "expected_executable": expected_executable,
            "installed_executable": installed_executable,
            "expected_arguments": expected_arguments,
            "installed_arguments": installed_arguments,
        }
    }


def _service_down(services: dict[str, dict[str, Any]]) -> list[str]:
    return [
        name for name in CRITICAL_SERVICES if str(_as_dict(services.get(name)).get("status") or "").lower() != "running"
    ]


def _missing_ports(ports: dict[int, dict[str, Any]]) -> list[int]:
    return [port for port in REQUIRED_PORTS if not bool(_as_dict(ports.get(port)).get("listening"))]


def _missing_dashboard_tasks(tasks: dict[str, dict[str, Any]]) -> list[str]:
    return [
        name
        for name in DASHBOARD_DURABLE_TASKS
        if str(_as_dict(tasks.get(name)).get("state") or "").lower() == "missing"
    ]


def _missing_paper_live_tasks(tasks: dict[str, dict[str, Any]]) -> list[str]:
    return [
        name
        for name in PAPER_LIVE_DURABLE_TASKS
        if str(_as_dict(tasks.get(name)).get("state") or "").lower() == "missing"
    ]


def _missing_data_pipeline_tasks(tasks: dict[str, dict[str, Any]]) -> list[str]:
    return [
        name
        for name in DATA_PIPELINE_TASKS
        if str(_as_dict(tasks.get(name)).get("state") or "").lower() == "missing"
    ]


def _stale_supervisor_restart_hooks(tasks: dict[str, dict[str, Any]]) -> list[str]:
    stale: list[str] = []
    task = _as_dict(tasks.get("ETA-Watchdog-Restart"))
    state = str(task.get("state") or "").lower()
    if state in {"", "missing", "disabled"}:
        return stale
    actions = str(task.get("actions") or "")
    if not actions:
        return stale
    if LEGACY_PAPERLIVE_SUPERVISOR_TASK in actions or CANONICAL_SUPERVISOR_TASK not in actions:
        stale.append("ETA-Watchdog-Restart")
    return stale


def _critical_endpoint_failures(endpoints: dict[str, dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for endpoint in ENDPOINTS:
        name = str(endpoint["name"])
        if bool(endpoint["critical"]) and not bool(_as_dict(endpoints.get(name)).get("ok")):
            failures.append(name)
    return failures


def _dashboard_schema_drift(endpoints: dict[str, dict[str, Any]]) -> list[str]:
    """Detect live dashboard processes still serving pre-alias diagnostics."""
    drifted: list[str] = []
    for name in ("local_dashboard_api_diagnostics", "local_dashboard_proxy_diagnostics"):
        endpoint = _as_dict(endpoints.get(name))
        if not bool(endpoint.get("ok")):
            continue
        payload = _as_dict(endpoint.get("payload"))
        if not payload:
            continue
        checks = _as_dict(payload.get("checks"))
        if (
            "hardening" not in payload
            or payload.get("hardening") != payload.get("vps_ops_hardening")
            or checks.get("hardening_contract") is not True
        ):
            drifted.append(name)
    return drifted


def _broker_gate_summary(broker_bracket_audit: dict[str, Any]) -> dict[str, Any]:
    raw_summary = broker_bracket_audit.get("summary")
    summary = _as_dict(raw_summary)
    position_summary = _as_dict(broker_bracket_audit.get("position_summary"))
    status = str(
        summary.get("status")
        or (raw_summary if isinstance(raw_summary, str) else None)
        or broker_bracket_audit.get("artifact_status")
        or "UNKNOWN"
    )
    symbols = [str(symbol) for symbol in _as_list(summary.get("missing_bracket_symbols")) if str(symbol)]
    if not symbols:
        symbols = [
            str(item.get("symbol"))
            for raw_item in _as_list(broker_bracket_audit.get("unprotected_positions"))
            if (item := _as_dict(raw_item)).get("symbol")
        ]
    ready = bool(
        summary.get("ready_for_prop_dry_run")
        or broker_bracket_audit.get("ready_for_prop_dry_run")
        or status in {"READY_NO_OPEN_EXPOSURE", "READY_OPEN_EXPOSURE_BRACKETED"}
    ) and status in {"PASS", "READY", "READY_NO_OPEN_EXPOSURE", "READY_OPEN_EXPOSURE_BRACKETED"}
    return {
        "status": status,
        "ready": ready,
        "missing_bracket_count": int(
            summary.get("missing_bracket_count") or position_summary.get("missing_bracket_count") or 0
        ),
        "missing_bracket_symbols": symbols,
    }


def _promotion_gate_summary(promotion_audit: dict[str, Any]) -> dict[str, Any]:
    raw_summary = promotion_audit.get("summary")
    summary = _as_dict(raw_summary)
    status = str(
        summary.get("status")
        or (raw_summary if isinstance(raw_summary, str) else None)
        or promotion_audit.get("artifact_status")
        or "UNKNOWN"
    )
    ready = bool(summary.get("ready_for_live") or promotion_audit.get("ready_for_prop_dry_run_review")) and status in {
        "PASS",
        "READY_FOR_PROP_DRY_RUN_REVIEW",
        "READY",
    }
    return {
        "status": status,
        "ready": ready,
        "required_evidence": [str(item) for item in _as_list(promotion_audit.get("required_evidence"))],
        "next_runner_candidate": _as_dict(promotion_audit.get("next_runner_candidate")),
    }


def _promotion_gate_action(gate: dict[str, Any]) -> str:
    runner = _as_dict(gate.get("next_runner_candidate"))
    runner_id = str(runner.get("bot_id") or "").strip()
    runner_symbol = str(runner.get("symbol") or "").strip()
    if runner_id:
        label = f"{runner_id} ({runner_symbol})" if runner_symbol else runner_id
        close_evidence = _as_dict(runner.get("broker_close_evidence"))
        if int(close_evidence.get("closed_trade_count") or 0) <= 0:
            outcome_evidence = _as_dict(runner.get("shadow_outcome_evidence"))
            outcome_count = int(outcome_evidence.get("evaluated_count") or 0)
            outcome_signal_count = int(outcome_evidence.get("shadow_signal_count") or 0)
            missing_bars = int(outcome_evidence.get("missing_bars") or 0)
            missing_context = int(outcome_evidence.get("missing_context") or 0)
            insufficient_future_bars = int(outcome_evidence.get("insufficient_future_bars") or 0)
            outcome_verdict = str(outcome_evidence.get("verdict") or "")
            if outcome_count <= 0 and outcome_signal_count > 0:
                if missing_context > 0:
                    return (
                        f"Capture fresh bracket-context shadow signals for runner-up {label}; "
                        f"{missing_context} older shadow signals lack planned entry/risk context"
                    )
                if missing_bars > 0:
                    return (
                        f"Repair bar freshness/source mapping for runner-up {label}; "
                        f"{missing_bars} shadow signals cannot replay into outcomes"
                    )
                if insufficient_future_bars > 0:
                    return f"Wait for enough future bars for runner-up {label}; replay window is incomplete"
                return f"Repair shadow outcome replay for runner-up {label}; signals exist but no outcomes evaluated"
            if outcome_count > 0:
                if outcome_verdict == "POSITIVE_COUNTERFACTUAL_EDGE":
                    return (
                        f"Move runner-up {label} into broker-paper close capture; "
                        "shadow replay is positive but not broker proof"
                    )
                if outcome_verdict in {"NO_COUNTERFACTUAL_EDGE", "WEAK_OR_NEGATIVE_COUNTERFACTUAL"}:
                    retune_plan = _as_dict(runner.get("retune_plan"))
                    retune_command = str(retune_plan.get("retune_command") or "").strip()
                    command_hint = f"; next: {retune_command}" if retune_command else ""
                    return (
                        f"Retune runner-up {label}; shadow replay is weak and "
                        f"broker-backed closes are missing{command_hint}"
                    )
                return f"Keep replaying runner-up {label} shadow outcomes until the sample is decisive"
            signal_evidence = _as_dict(runner.get("shadow_signal_evidence"))
            if int(signal_evidence.get("signal_count") or 0) > 0:
                return (
                    f"Convert runner-up {label} shadow signals into paper-close outcomes; "
                    "broker-backed closes are still missing"
                )
            watch_evidence = _as_dict(runner.get("supervisor_watch_evidence"))
            if watch_evidence.get("verdict") == "WATCHING_NO_SIGNAL_YET":
                return f"Keep runner-up {label} in paper watch; supervisor is live but no signal has fired yet"
            return f"Collect broker-backed closes for runner-up {label} before any prop promotion"
        return f"Keep strategy lane in paper soak; evaluate runner-up {label} before any prop promotion"
    required = [str(item) for item in _as_list(gate.get("required_evidence")) if str(item).strip()]
    if required:
        return "Keep strategy lane in paper soak: " + required[0]
    return "Keep strategy lane in paper soak until prop promotion audit is PASS/READY"


def _paper_live_status(
    *,
    runtime_ready: bool,
    broker_gate: dict[str, Any],
    ibgateway_gate: dict[str, Any],
    supervisor_reconcile_gate: dict[str, Any],
    supervisor_code_gate: dict[str, Any],
) -> str:
    if not runtime_ready:
        return "BLOCKED_RUNTIME"
    if not broker_gate["ready"]:
        return "BLOCKED_BROKER_BRACKETS"
    if not ibgateway_gate["ready"]:
        return "BLOCKED_IBKR_GATEWAY"
    if not supervisor_reconcile_gate["ready"]:
        return "BLOCKED_SUPERVISOR_RECONCILE"
    if not supervisor_code_gate["ready"]:
        return "BLOCKED_SUPERVISOR_CODE"
    return "READY_FOR_PAPER_SOAK"


def _ibgateway_summary(
    ibgateway_reauth: dict[str, Any] | None,
    ports: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    if ibgateway_reauth is None:
        return {
            "status": "NOT_COLLECTED",
            "ready": True,
            "port": 4002,
            "port_listening": bool(_as_dict(ports.get(4002)).get("listening")),
            "reason": None,
        }
    status = str(ibgateway_reauth.get("status") or ibgateway_reauth.get("artifact_status") or "UNKNOWN")
    port_listening = bool(_as_dict(ports.get(4002)).get("listening"))
    ready = status == "healthy" and port_listening
    authority = _as_dict(ibgateway_reauth.get("gateway_authority"))
    return {
        "status": status,
        "ready": ready,
        "port": 4002,
        "port_listening": port_listening,
        "reason": ibgateway_reauth.get("reason"),
        "checked_at": ibgateway_reauth.get("checked_at"),
        "gateway_authority": authority,
        "non_authoritative_host": status == "non_authoritative_gateway_host"
        or authority.get("allowed") is False,
    }


def _symbols_from_rows(rows: list[Any]) -> list[str]:
    symbols: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip()
        if symbol:
            symbols.append(symbol)
    return sorted(dict.fromkeys(symbols))


def _float_value(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _position_symbol_root(value: object) -> str:
    symbol = str(value or "").strip().upper().replace("/", "")
    if not symbol:
        return ""
    for suffix in ("USDT", "USD"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break
    if (
        len(symbol) >= 3
        and symbol[-1].isdigit()
        and symbol[-1] != "1"
        and symbol[-2] in FUTURES_MONTH_CODES
    ):
        return symbol[:-2]
    return symbol.rstrip("0123456789") or symbol


def _broker_positions_by_root(live_broker_state: dict[str, Any] | None) -> dict[str, float]:
    live_broker_state = live_broker_state if isinstance(live_broker_state, dict) else {}
    by_root: dict[str, float] = {}
    for venue in ("ibkr", "tastytrade", "tasty"):
        venue_state = _as_dict(live_broker_state.get(venue))
        for row in _as_list(venue_state.get("open_positions")):
            if not isinstance(row, dict):
                continue
            root = _position_symbol_root(row.get("symbol") or row.get("local_symbol") or row.get("contract"))
            qty = _float_value(row.get("position"))
            if qty is None:
                qty = _float_value(row.get("qty"))
            if qty is None:
                qty = _float_value(row.get("quantity"))
            if not root or qty is None or abs(qty) <= 1e-6:
                continue
            by_root[root] = by_root.get(root, 0.0) + qty
    return by_root


def _supervisor_positions_by_root(supervisor_heartbeat: dict[str, Any] | None) -> dict[str, float]:
    supervisor_heartbeat = supervisor_heartbeat if isinstance(supervisor_heartbeat, dict) else {}
    by_root: dict[str, float] = {}
    for bot in _as_list(supervisor_heartbeat.get("bots")):
        if not isinstance(bot, dict):
            continue
        open_position = _as_dict(bot.get("open_position"))
        if not open_position:
            continue
        root = _position_symbol_root(bot.get("symbol") or open_position.get("symbol"))
        qty = _float_value(open_position.get("qty"))
        if qty is None:
            qty = _float_value(open_position.get("quantity"))
        if not root or qty is None or abs(qty) <= 1e-6:
            continue
        side = str(open_position.get("side") or open_position.get("direction") or "BUY").upper()
        signed = abs(qty) if side not in {"SELL", "SHORT"} else -abs(qty)
        by_root[root] = by_root.get(root, 0.0) + signed
    return by_root


def _diff_position_books(
    *,
    broker_by_root: dict[str, float],
    supervisor_by_root: dict[str, float],
    checked_at: str,
    source: str,
    brokers_queried: list[str],
) -> dict[str, Any]:
    findings: dict[str, Any] = {
        "checked_at": checked_at,
        "source": source,
        "broker_only": [],
        "supervisor_only": [],
        "divergent": [],
        "matched": 0,
        "brokers_queried": brokers_queried,
    }
    for root in sorted(set(broker_by_root) | set(supervisor_by_root)):
        b_qty = float(broker_by_root.get(root, 0.0))
        s_qty = float(supervisor_by_root.get(root, 0.0))
        diff = abs(b_qty - s_qty)
        if diff <= 1e-6:
            findings["matched"] += 1
        elif abs(s_qty) <= 1e-6:
            findings["broker_only"].append({"symbol": root, "broker_qty": b_qty})
        elif abs(b_qty) <= 1e-6:
            findings["supervisor_only"].append({"symbol": root, "supervisor_qty": s_qty})
        else:
            findings["divergent"].append(
                {
                    "symbol": root,
                    "broker_qty": b_qty,
                    "supervisor_qty": s_qty,
                    "delta": b_qty - s_qty,
                }
            )
    return findings


def _reconcile_mismatch_count(snapshot: dict[str, Any] | None) -> int:
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    return (
        len(_as_list(snapshot.get("broker_only")))
        + len(_as_list(snapshot.get("supervisor_only")))
        + len(_as_list(snapshot.get("divergent")))
    )


def _current_supervisor_reconcile_snapshot(
    *,
    supervisor_heartbeat: dict[str, Any] | None,
    live_broker_state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(live_broker_state, dict) or not live_broker_state.get("ready"):
        return None
    broker_by_root = _broker_positions_by_root(live_broker_state)
    supervisor_by_root = _supervisor_positions_by_root(supervisor_heartbeat)
    if not broker_by_root and not supervisor_by_root:
        return None
    ibkr_state = _as_dict(live_broker_state.get("ibkr"))
    reported_open_count = _float_value(live_broker_state.get("open_position_count"))
    if reported_open_count is None:
        reported_open_count = _float_value(ibkr_state.get("open_position_count"))
    if not broker_by_root and reported_open_count is not None and reported_open_count > 0:
        return None
    if not broker_by_root and supervisor_by_root and reported_open_count is None:
        return None
    snapshot = _diff_position_books(
        broker_by_root=broker_by_root,
        supervisor_by_root=supervisor_by_root,
        checked_at=datetime.now(UTC).isoformat(),
        source="supervisor_heartbeat_and_live_broker_state",
        brokers_queried=["ibkr"] if _as_dict((live_broker_state or {}).get("ibkr")).get("ready") else [],
    )
    snapshot["broker_roots"] = dict(sorted(broker_by_root.items()))
    snapshot["supervisor_roots"] = dict(sorted(supervisor_by_root.items()))
    snapshot["heartbeat_ts"] = _as_dict(supervisor_heartbeat).get("ts")
    snapshot["broker_state_source"] = _as_dict(live_broker_state).get("source")
    return snapshot


def _effective_supervisor_reconcile(
    *,
    supervisor_reconcile: dict[str, Any] | None,
    supervisor_heartbeat: dict[str, Any] | None,
    live_broker_state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    current = _current_supervisor_reconcile_snapshot(
        supervisor_heartbeat=supervisor_heartbeat,
        live_broker_state=live_broker_state,
    )
    if current is None:
        return supervisor_reconcile
    if _reconcile_mismatch_count(current):
        return current
    # A clean current read should not silently clear a supervisor startup
    # divergence latch; the operator still needs to clear that runtime hold.
    if _reconcile_mismatch_count(supervisor_reconcile):
        return supervisor_reconcile
    return current


def _supervisor_code_summary(
    *,
    supervisor_heartbeat: dict[str, Any] | None,
    repo_revision: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fail closed when the running supervisor is older than deployed code."""
    if repo_revision is None:
        return {
            "status": "NOT_COLLECTED",
            "ready": True,
    }
    heartbeat = supervisor_heartbeat if isinstance(supervisor_heartbeat, dict) else {}
    heartbeat_revision = _as_dict(heartbeat.get("code_revision"))
    heartbeat_head = str(heartbeat_revision.get("head") or "").strip()
    repo_head = str(repo_revision.get("head") or "").strip()
    heartbeat_head_short = str(heartbeat_revision.get("head_short") or heartbeat_head[:7])
    repo_head_short = str(repo_revision.get("head_short") or repo_head[:7])
    ready = bool(repo_head and heartbeat_head and heartbeat_head == repo_head)
    if not repo_head:
        status = "REPO_REVISION_UNKNOWN"
    elif heartbeat.get("artifact_status") in {"missing", "unreadable", "invalid"}:
        status = "MISSING_SUPERVISOR_HEARTBEAT"
    elif not heartbeat_head:
        status = "MISSING_SUPERVISOR_CODE_REVISION"
    elif heartbeat_head != repo_head:
        status = "STALE_SUPERVISOR_CODE"
    else:
        status = "PASS"
    return {
        "status": status,
        "ready": ready,
        "heartbeat_head": heartbeat_head,
        "repo_head": repo_head,
        "heartbeat_head_short": heartbeat_head_short,
        "repo_head_short": repo_head_short,
        "heartbeat_repo_root": heartbeat_revision.get("repo_root"),
        "repo_root": repo_revision.get("repo_root"),
        "heartbeat_captured_at": heartbeat_revision.get("captured_at"),
        "repo_captured_at": repo_revision.get("captured_at"),
        "heartbeat_dirty": heartbeat_revision.get("dirty"),
        "repo_dirty": repo_revision.get("dirty"),
        "heartbeat_ts": heartbeat.get("ts"),
    }


def _supervisor_code_action(gate: dict[str, Any]) -> str:
    status = str(gate.get("status") or "unknown")
    if status == "STALE_SUPERVISOR_CODE":
        return (
            "Restart ETA-Jarvis-Strategy-Supervisor so paper-live supervisor loads deployed code "
            f"({gate.get('heartbeat_head_short') or gate.get('heartbeat_head')} -> "
            f"{gate.get('repo_head_short') or gate.get('repo_head')})"
        )
    if status == "MISSING_SUPERVISOR_CODE_REVISION":
        return (
            "Restart ETA-Jarvis-Strategy-Supervisor so heartbeat includes code_revision "
            "before clearing paper-live trading gates"
        )
    if status == "MISSING_SUPERVISOR_HEARTBEAT":
        return "Start or repair ETA-Jarvis-Strategy-Supervisor; canonical supervisor heartbeat is missing"
    return f"Review supervisor code revision gate: {status}"


def _supervisor_reconcile_summary(supervisor_reconcile: dict[str, Any] | None) -> dict[str, Any]:
    """Summarize broker-vs-supervisor position reconciliation as a safety gate."""
    if supervisor_reconcile is None:
        return {
            "status": "not_collected",
            "ready": True,
            "checked_at": None,
            "age_s": None,
            "max_age_s": SUPERVISOR_RECONCILE_MAX_AGE_S,
            "broker_only_symbols": [],
            "supervisor_only_symbols": [],
            "divergent_symbols": [],
            "mismatch_count": 0,
            "brokers_queried": [],
        }

    artifact_status = str(supervisor_reconcile.get("artifact_status") or "").strip()
    checked_at = supervisor_reconcile.get("checked_at") or supervisor_reconcile.get("generated_at_utc")
    age_s = _iso_age_seconds(checked_at)
    broker_only = _as_list(supervisor_reconcile.get("broker_only"))
    supervisor_only = _as_list(supervisor_reconcile.get("supervisor_only"))
    divergent = _as_list(supervisor_reconcile.get("divergent"))
    broker_only_symbols = _symbols_from_rows(broker_only)
    supervisor_only_symbols = _symbols_from_rows(supervisor_only)
    divergent_symbols = _symbols_from_rows(divergent)
    mismatch_count = len(broker_only) + len(supervisor_only) + len(divergent)

    if artifact_status:
        status = f"{artifact_status}_reconcile_snapshot"
        ready = False
    elif age_s is None or age_s > SUPERVISOR_RECONCILE_MAX_AGE_S:
        status = "STALE_RECONCILE_SNAPSHOT"
        ready = False
    elif mismatch_count:
        status = "BLOCKED_BROKER_SUPERVISOR_RECONCILE"
        ready = False
    else:
        status = "PASS"
        ready = True

    return {
        "status": status,
        "ready": ready,
        "source": supervisor_reconcile.get("source") or "reconcile_artifact",
        "path": supervisor_reconcile.get("path"),
        "checked_at": checked_at,
        "age_s": round(age_s, 1) if age_s is not None else None,
        "max_age_s": SUPERVISOR_RECONCILE_MAX_AGE_S,
        "heartbeat_ts": supervisor_reconcile.get("heartbeat_ts"),
        "broker_state_source": supervisor_reconcile.get("broker_state_source"),
        "broker_only": broker_only,
        "supervisor_only": supervisor_only,
        "divergent": divergent,
        "broker_only_symbols": broker_only_symbols,
        "supervisor_only_symbols": supervisor_only_symbols,
        "divergent_symbols": divergent_symbols,
        "mismatch_count": mismatch_count,
        "brokers_queried": [
            str(broker) for broker in _as_list(supervisor_reconcile.get("brokers_queried")) if str(broker)
        ],
    }


def _supervisor_reconcile_action(gate: dict[str, Any]) -> str:
    status = str(gate.get("status") or "unknown")
    if status == "BLOCKED_BROKER_SUPERVISOR_RECONCILE":
        parts: list[str] = []
        broker_only = ", ".join(str(symbol) for symbol in _as_list(gate.get("broker_only_symbols")))
        supervisor_only = ", ".join(str(symbol) for symbol in _as_list(gate.get("supervisor_only_symbols")))
        divergent = ", ".join(str(symbol) for symbol in _as_list(gate.get("divergent_symbols")))
        if broker_only:
            parts.append(f"broker-only: {broker_only}")
        if supervisor_only:
            parts.append(f"supervisor-only: {supervisor_only}")
        if divergent:
            parts.append(f"divergent: {divergent}")
        detail = "; ".join(parts) if parts else f"{int(gate.get('mismatch_count') or 0)} mismatch(es)"
        return (
            "Do not unlock new entries: reconcile broker/supervisor positions "
            f"({detail}) before clearing the supervisor entry halt"
        )
    if status == "STALE_RECONCILE_SNAPSHOT":
        return "Refresh supervisor broker reconciliation before clearing the supervisor entry halt"
    return f"Repair supervisor broker reconciliation safety gate: {status}"


def _jarvis_hermes_admin_summary(
    admin_audit: dict[str, Any] | None,
) -> dict[str, Any]:
    """Summarize the read-only Jarvis/Hermes admin-AI readiness audit."""
    if admin_audit is None:
        return {
            "status": "NOT_COLLECTED",
            "ready": True,
            "admin_ai_ready": True,
            "blocked": 0,
            "warnings": 0,
            "next_actions": [],
        }
    status = str(admin_audit.get("status") or "UNKNOWN")
    summary = _as_dict(admin_audit.get("summary"))
    next_actions = [str(action) for action in _as_list(admin_audit.get("next_actions"))]
    ready = status == "PASS" and bool(summary.get("admin_ai_ready"))
    return {
        "status": status,
        "ready": ready,
        "admin_ai_ready": ready,
        "blocked": int(summary.get("blocked") or 0),
        "warnings": int(summary.get("warnings") or 0),
        "checks": int(summary.get("checks") or 0),
        "pass": int(summary.get("pass") or 0),
        "order_action_allowed": False,
        "live_money_gate_bypassed": False,
        "next_actions": next_actions,
    }


def _config_drift(service_config: dict[str, dict[str, Any]]) -> list[str]:
    return [name for name, config in service_config.items() if not bool(_as_dict(config).get("matches_expected"))]


def build_report(
    *,
    services: dict[str, dict[str, Any]],
    ports: dict[int, dict[str, Any]],
    endpoints: dict[str, dict[str, Any]],
    broker_bracket_audit: dict[str, Any],
    promotion_audit: dict[str, Any],
    service_config: dict[str, dict[str, Any]],
    tasks: dict[str, dict[str, Any]] | None = None,
    ibgateway_reauth: dict[str, Any] | None = None,
    jarvis_hermes_admin: dict[str, Any] | None = None,
    supervisor_reconcile: dict[str, Any] | None = None,
    supervisor_heartbeat: dict[str, Any] | None = None,
    live_broker_state: dict[str, Any] | None = None,
    repo_revision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the deterministic hardening report from collected inputs."""
    tasks = tasks or {}
    service_down = _service_down(services)
    missing_ports = _missing_ports(ports)
    missing_dashboard_tasks = _missing_dashboard_tasks(tasks)
    missing_paper_live_tasks = _missing_paper_live_tasks(tasks)
    missing_data_pipeline_tasks = _missing_data_pipeline_tasks(tasks)
    stale_restart_hooks = _stale_supervisor_restart_hooks(tasks)
    endpoint_failures = _critical_endpoint_failures(endpoints)
    dashboard_schema_drift = _dashboard_schema_drift(endpoints)
    drifted_configs = _config_drift(service_config)
    broker_gate = _broker_gate_summary(broker_bracket_audit)
    promotion_gate = _promotion_gate_summary(promotion_audit)
    ibgateway_gate = _ibgateway_summary(ibgateway_reauth, ports)
    admin_ai_gate = _jarvis_hermes_admin_summary(jarvis_hermes_admin)
    effective_supervisor_reconcile = _effective_supervisor_reconcile(
        supervisor_reconcile=supervisor_reconcile,
        supervisor_heartbeat=supervisor_heartbeat,
        live_broker_state=live_broker_state,
    )
    supervisor_reconcile_gate = _supervisor_reconcile_summary(effective_supervisor_reconcile)
    supervisor_code_gate = _supervisor_code_summary(
        supervisor_heartbeat=supervisor_heartbeat,
        repo_revision=repo_revision,
    )
    runtime_ready = (
        not service_down
        and not missing_ports
        and not endpoint_failures
        and not missing_paper_live_tasks
        and not missing_data_pipeline_tasks
        and not stale_restart_hooks
    )
    dashboard_durable = not missing_dashboard_tasks
    paper_live_status = _paper_live_status(
        runtime_ready=runtime_ready,
        broker_gate=broker_gate,
        ibgateway_gate=ibgateway_gate,
        supervisor_reconcile_gate=supervisor_reconcile_gate,
        supervisor_code_gate=supervisor_code_gate,
    )
    paper_live_gate_ready = paper_live_status == "READY_FOR_PAPER_SOAK"
    prop_promotion_gate_ready = bool(promotion_gate["ready"])
    live_promotion_blocked = not prop_promotion_gate_ready
    trading_gate_ready = bool(paper_live_gate_ready and prop_promotion_gate_ready)
    next_actions: list[str] = []

    for name in service_down:
        next_actions.append(f"Start or repair Windows service: {name}")
    for port in missing_ports:
        next_actions.append(f"Restore listener on port {port}")
    for name in endpoint_failures:
        next_actions.append(f"Repair critical endpoint probe: {name}")
    if missing_dashboard_tasks:
        next_actions.append(
            "Run elevated dashboard durability repair: "
            "eta_engine\\deploy\\scripts\\repair_dashboard_durability_admin.cmd "
            "(registers " + ", ".join(missing_dashboard_tasks) + ")"
        )
    if missing_paper_live_tasks:
        next_actions.append(
            "Repair paper-live scheduled task lane: " + ", ".join(missing_paper_live_tasks)
        )
    if missing_data_pipeline_tasks:
        next_actions.append(
            "Repair data-pipeline scheduled task lane: " + ", ".join(missing_data_pipeline_tasks)
        )
    if stale_restart_hooks:
        next_actions.append(
            "Disable or repair stale supervisor restart hook(s): "
            + ", ".join(stale_restart_hooks)
            + f"; target {CANONICAL_SUPERVISOR_TASK} only"
        )
    if dashboard_schema_drift:
        next_actions.append(
            "Reload dashboard API/proxy so live diagnostics schema includes the hardening alias: "
            + ", ".join(dashboard_schema_drift)
        )
    if drifted_configs:
        next_actions.append("Run an elevated restart/install for drifted WinSW services: " + ", ".join(drifted_configs))
    if not ibgateway_gate["ready"]:
        if ibgateway_gate["status"] == "missing_ibc_credentials":
            next_actions.append(
                "Seed IBC credentials with deploy\\scripts\\set_ibc_credentials.ps1 "
                "-PromptForPassword; keep IBKR read-only until 127.0.0.1:4002 listens"
            )
        elif ibgateway_gate["status"] == "non_authoritative_gateway_host":
            next_actions.append(
                "Do not enable local desktop Gateway tasks; verify the VPS authority marker and Gateway recovery lane "
                "on the 24/7 server."
            )
        elif not ibgateway_gate["port_listening"]:
            next_actions.append(
                "Keep IBKR unavailable until Gateway API port 4002 is listening "
                "and ibgateway_reauth.json reports healthy"
            )
        else:
            next_actions.append("Repair IBKR Gateway readiness state before any broker promotion")
    missing_symbols = ", ".join(broker_gate["missing_bracket_symbols"])
    if missing_symbols:
        next_actions.append(f"Do not promote: verify native broker brackets/OCO protection for {missing_symbols}")
    elif broker_gate["missing_bracket_count"]:
        next_actions.append(
            "Do not promote: verify native broker brackets/OCO protection for "
            f"{broker_gate['missing_bracket_count']} unprotected broker position(s)"
        )
    if not supervisor_reconcile_gate["ready"]:
        next_actions.append(_supervisor_reconcile_action(supervisor_reconcile_gate))
    if not supervisor_code_gate["ready"]:
        next_actions.append(_supervisor_code_action(supervisor_code_gate))
    if not promotion_gate["ready"]:
        next_actions.append(_promotion_gate_action(promotion_gate))
    if not admin_ai_gate["ready"]:
        admin_actions = admin_ai_gate["next_actions"]
        if admin_actions:
            next_actions.append("Review Jarvis/Hermes admin-AI readiness: " + str(admin_actions[0]))
        else:
            next_actions.append("Review Jarvis/Hermes admin-AI readiness before fully unlocking VPS admin AI")

    if not runtime_ready:
        status = "RED_RUNTIME_DEGRADED"
    elif drifted_configs or (dashboard_schema_drift and trading_gate_ready):
        status = "YELLOW_RESTART_REQUIRED"
    elif not dashboard_durable and trading_gate_ready:
        status = "YELLOW_DURABILITY_GAP"
    elif not trading_gate_ready:
        status = "YELLOW_SAFETY_BLOCKED"
    elif admin_ai_gate["status"] == "BLOCKED":
        status = "YELLOW_ADMIN_AI_BLOCKED"
    elif admin_ai_gate["status"] == "WARN":
        status = "YELLOW_ADMIN_AI_PENDING"
    else:
        status = "GREEN_READY_FOR_SOAK"

    promotion_allowed = status == "GREEN_READY_FOR_SOAK" and trading_gate_ready
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "summary": {
            "status": status,
            "runtime_ready": runtime_ready,
            "dashboard_durable": dashboard_durable,
            "dashboard_schema_current": not dashboard_schema_drift,
            "paper_live_gate_ready": paper_live_gate_ready,
            "paper_live_status": paper_live_status,
            "trading_gate_ready": trading_gate_ready,
            "prop_promotion_gate_ready": prop_promotion_gate_ready,
            "live_promotion_blocked": live_promotion_blocked,
            "admin_ai_ready": admin_ai_gate["ready"],
            "admin_ai_status": admin_ai_gate["status"],
            "supervisor_reconcile_ready": supervisor_reconcile_gate["ready"],
            "supervisor_code_ready": supervisor_code_gate["ready"],
            "supervisor_code_current": supervisor_code_gate["ready"],
            "promotion_allowed": promotion_allowed,
            "order_action_allowed": False,
        },
        "runtime": {
            "services": {
                "critical": list(CRITICAL_SERVICES),
                "optional": list(OPTIONAL_SERVICES),
                "legacy_compatibility": list(LEGACY_COMPAT_SERVICES),
                "down": service_down,
                "observed": services,
            },
            "ports": {
                "required": list(REQUIRED_PORTS),
                "missing": missing_ports,
                "observed": ports,
            },
            "endpoints": {
                "critical_failures": endpoint_failures,
                "schema_drift": dashboard_schema_drift,
                "observed": endpoints,
            },
            "tasks": {
                "dashboard_durable": list(DASHBOARD_DURABLE_TASKS),
                "paper_live_durable": list(PAPER_LIVE_DURABLE_TASKS),
                "data_pipeline": list(DATA_PIPELINE_TASKS),
                "ibgateway": list(IBGATEWAY_TASKS),
                "watchdog_observed": list(WATCHDOG_OBSERVED_TASKS),
                "missing_dashboard_durable": missing_dashboard_tasks,
                "missing_paper_live_durable": missing_paper_live_tasks,
                "missing_data_pipeline": missing_data_pipeline_tasks,
                "stale_supervisor_restart_hooks": stale_restart_hooks,
                "observed": tasks,
            },
        },
        "broker_runtime": {
            "ibgateway": ibgateway_gate,
        },
        "safety_gates": {
            "broker_brackets": broker_gate,
            "promotion": promotion_gate,
            "supervisor_reconcile": supervisor_reconcile_gate,
            "supervisor_code": supervisor_code_gate,
            "jarvis_hermes_admin_ai": admin_ai_gate,
        },
        "service_config": service_config,
        "next_actions": list(dict.fromkeys(next_actions)),
    }


def collect_jarvis_hermes_admin_status() -> dict[str, Any]:
    """Collect the current Jarvis/Hermes admin-AI audit without side effects."""
    return jarvis_hermes_admin_audit.run_audit(
        workspace_roots.WORKSPACE_ROOT,
        expected_task_count=CURRENT_JARVIS_HERMES_BRIDGE_TASK_COUNT,
        probe_port=True,
    )


def collect_live_report() -> dict[str, Any]:
    """Collect live read-only inputs and build the hardening report."""
    broker_state_probe = _probe_endpoint(BROKER_STATE_URL, timeout_s=8.0)
    live_broker_state = _as_dict(broker_state_probe.get("payload"))
    return build_report(
        services=collect_service_status(),
        ports=collect_port_status(),
        endpoints=collect_endpoint_status(),
        broker_bracket_audit=_read_json(workspace_roots.ETA_BROKER_BRACKET_AUDIT_PATH),
        promotion_audit=_read_json(workspace_roots.ETA_PROP_STRATEGY_PROMOTION_AUDIT_PATH),
        service_config=collect_service_config_status(),
        tasks=collect_task_status(),
        ibgateway_reauth=_read_json(workspace_roots.ETA_RUNTIME_STATE_DIR / "ibgateway_reauth.json"),
        jarvis_hermes_admin=collect_jarvis_hermes_admin_status(),
        supervisor_reconcile=_read_json(workspace_roots.ETA_JARVIS_SUPERVISOR_RECONCILE_PATH),
        supervisor_heartbeat=_read_json(workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH),
        live_broker_state=live_broker_state,
        repo_revision=collect_repo_revision(),
    )


def _print_human(report: dict[str, Any]) -> None:
    summary = _as_dict(report.get("summary"))
    print(f"VPS ops hardening: {summary.get('status', 'UNKNOWN')}")
    print(f"Runtime ready: {summary.get('runtime_ready')}")
    print(f"Dashboard durable: {summary.get('dashboard_durable')}")
    print(f"Paper-live gate ready: {summary.get('paper_live_gate_ready')} ({summary.get('paper_live_status')})")
    print(f"Trading gate ready: {summary.get('trading_gate_ready')}")
    print(f"Supervisor code current: {summary.get('supervisor_code_current')}")
    print(f"Admin AI ready: {summary.get('admin_ai_ready')} ({summary.get('admin_ai_status')})")
    print(f"Promotion allowed: {summary.get('promotion_allowed')}")
    print("Order action allowed: False")
    actions = _as_list(report.get("next_actions"))
    if not actions:
        print("No current hardening actions.")
        return
    print()
    print("Next actions:")
    for action in actions:
        print(f"- {action}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vps_ops_hardening_audit")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--json-out",
        nargs="?",
        const=str(DEFAULT_OUT),
        help="write the audit JSON; defaults to canonical var/eta_engine/state path",
    )
    args = parser.parse_args(argv)

    report = collect_live_report()
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)
    return 1 if str(_as_dict(report.get("summary")).get("status")).startswith("RED") else 0


if __name__ == "__main__":
    raise SystemExit(main())
