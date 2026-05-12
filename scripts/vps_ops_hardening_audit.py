"""Read-only VPS operations hardening audit.

This is an operator-facing health view, not a trading actuator. It can say
"the VPS/runtime is alive" while still keeping promotion blocked when broker
brackets, paper-soak, or prop gates are not clean yet.
"""

from __future__ import annotations

import argparse
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
CURRENT_JARVIS_HERMES_BRIDGE_TASK_COUNT = 8
CRITICAL_SERVICES = (
    "FirmCommandCenter",
    "FirmCommandCenterEdge",
    "FirmCommandCenterTunnel",
    "FirmCore",
    "FirmWatchdog",
    "ETAJarvisSupervisor",
    "FmStatusServer",
)
OPTIONAL_SERVICES = ("HermesJarvisTelegram",)
REQUIRED_PORTS = (8000, 8420, 8421, 8422)
BROKER_PORTS = (4002,)
DASHBOARD_DURABLE_TASKS = (
    "ETA-Dashboard-API",
    "ETA-Proxy-8421",
    "ETA-Dashboard-Proxy-Watchdog",
    "ETA-OperatorQueueHeartbeat",
    "ETA-PaperLiveTransitionCheck",
)
IBGATEWAY_TASKS = (
    "ETA-IBGateway",
    "ETA-IBGateway-Autostart",
    "ETA-IBGateway-DailyRestart",
    "ETA-IBGateway-RunNow",
)
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
        "name": "local_command_center_master",
        "url": "http://127.0.0.1:8420/api/master/status",
        "critical": False,
        "timeout_s": 15.0,
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


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"artifact_status": "missing", "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"artifact_status": "unreadable", "path": str(path), "error": str(exc)}
    return payload if isinstance(payload, dict) else {"artifact_status": "invalid", "path": str(path)}


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
    payload = _run_powershell_json(command)
    if isinstance(payload, dict) and "error" in payload:
        return {
            name: {"name": name, "status": "Unknown", "error": payload["error"]}
            for name in names
        }
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
    payload = _run_powershell_json(command)
    if isinstance(payload, dict) and "error" in payload:
        return {
            port: {"port": port, "listening": False, "error": payload["error"], "owners": []}
            for port in REQUIRED_PORTS
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
    names = list(DASHBOARD_DURABLE_TASKS + IBGATEWAY_TASKS + ("ETA-Autopilot",))
    quoted = ", ".join(f"'{name}'" for name in names)
    command = f"""
$names = @({quoted})
$results = foreach ($name in $names) {{
  $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
  if ($null -eq $task) {{
    [pscustomobject]@{{TaskName=$name;State='Missing';LastTaskResult=$null;LastRunTime=$null;NextRunTime=$null}}
  }} else {{
    $info = Get-ScheduledTaskInfo -TaskName $name -ErrorAction SilentlyContinue
    [pscustomobject]@{{
      TaskName=$task.TaskName
      State=[string]$task.State
      LastTaskResult=$info.LastTaskResult
      LastRunTime=$info.LastRunTime
      NextRunTime=$info.NextRunTime
    }}
  }}
}}
$results | ConvertTo-Json -Depth 4
"""
    payload = _run_powershell_json(command)
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
        name
        for name in CRITICAL_SERVICES
        if str(_as_dict(services.get(name)).get("status") or "").lower() != "running"
    ]


def _missing_ports(ports: dict[int, dict[str, Any]]) -> list[int]:
    return [
        port
        for port in REQUIRED_PORTS
        if not bool(_as_dict(ports.get(port)).get("listening"))
    ]


def _missing_dashboard_tasks(tasks: dict[str, dict[str, Any]]) -> list[str]:
    return [
        name
        for name in DASHBOARD_DURABLE_TASKS
        if str(_as_dict(tasks.get(name)).get("state") or "").lower() == "missing"
    ]


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
    symbols = [
        str(symbol)
        for symbol in _as_list(summary.get("missing_bracket_symbols"))
        if str(symbol)
    ]
    if not symbols:
        symbols = [
            str(item.get("symbol"))
            for raw_item in _as_list(broker_bracket_audit.get("unprotected_positions"))
            if (item := _as_dict(raw_item)).get("symbol")
        ]
    ready = bool(
        summary.get("ready_for_prop_dry_run")
        or broker_bracket_audit.get("ready_for_prop_dry_run")
        or status in {"READY_NO_OPEN_EXPOSURE"}
    ) and status in {"PASS", "READY", "READY_NO_OPEN_EXPOSURE"}
    return {
        "status": status,
        "ready": ready,
        "missing_bracket_count": int(
            summary.get("missing_bracket_count")
            or position_summary.get("missing_bracket_count")
            or 0
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
    ready = bool(
        summary.get("ready_for_live")
        or promotion_audit.get("ready_for_prop_dry_run_review")
    ) and status in {
        "PASS",
        "READY_FOR_PROP_DRY_RUN_REVIEW",
        "READY",
    }
    return {"status": status, "ready": ready}


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
    status = str(
        ibgateway_reauth.get("status")
        or ibgateway_reauth.get("artifact_status")
        or "UNKNOWN"
    )
    port_listening = bool(_as_dict(ports.get(4002)).get("listening"))
    ready = status == "healthy" and port_listening
    return {
        "status": status,
        "ready": ready,
        "port": 4002,
        "port_listening": port_listening,
        "reason": ibgateway_reauth.get("reason"),
        "checked_at": ibgateway_reauth.get("checked_at"),
    }


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
    return [
        name
        for name, config in service_config.items()
        if not bool(_as_dict(config).get("matches_expected"))
    ]


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
) -> dict[str, Any]:
    """Build the deterministic hardening report from collected inputs."""
    tasks = tasks or {}
    service_down = _service_down(services)
    missing_ports = _missing_ports(ports)
    missing_dashboard_tasks = _missing_dashboard_tasks(tasks)
    endpoint_failures = _critical_endpoint_failures(endpoints)
    dashboard_schema_drift = _dashboard_schema_drift(endpoints)
    drifted_configs = _config_drift(service_config)
    broker_gate = _broker_gate_summary(broker_bracket_audit)
    promotion_gate = _promotion_gate_summary(promotion_audit)
    ibgateway_gate = _ibgateway_summary(ibgateway_reauth, ports)
    admin_ai_gate = _jarvis_hermes_admin_summary(jarvis_hermes_admin)
    runtime_ready = not service_down and not missing_ports and not endpoint_failures
    dashboard_durable = not missing_dashboard_tasks
    trading_gate_ready = bool(
        broker_gate["ready"]
        and promotion_gate["ready"]
        and ibgateway_gate["ready"]
    )
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
            "(registers "
            + ", ".join(missing_dashboard_tasks)
            + ")"
        )
    if dashboard_schema_drift:
        next_actions.append(
            "Reload dashboard API/proxy so live diagnostics schema includes the hardening alias: "
            + ", ".join(dashboard_schema_drift)
        )
    if drifted_configs:
        next_actions.append(
            "Run an elevated restart/install for drifted WinSW services: "
            + ", ".join(drifted_configs)
        )
    if not ibgateway_gate["ready"]:
        if ibgateway_gate["status"] == "missing_ibc_credentials":
            next_actions.append(
                "Seed IBC credentials with deploy\\scripts\\set_ibc_credentials.ps1 "
                "-PromptForPassword; keep IBKR read-only until 127.0.0.1:4002 listens"
            )
        elif not ibgateway_gate["port_listening"]:
            next_actions.append(
                "Keep IBKR unavailable until Gateway API port 4002 is listening "
                "and ibgateway_reauth.json reports healthy"
            )
        else:
            next_actions.append(
                "Repair IBKR Gateway readiness state before any broker promotion"
            )
    missing_symbols = ", ".join(broker_gate["missing_bracket_symbols"])
    if missing_symbols:
        next_actions.append(
            "Do not promote: verify native broker brackets/OCO protection for "
            f"{missing_symbols}"
        )
    elif broker_gate["missing_bracket_count"]:
        next_actions.append(
            "Do not promote: verify native broker brackets/OCO protection for "
            f"{broker_gate['missing_bracket_count']} unprotected broker position(s)"
        )
    if not promotion_gate["ready"]:
        next_actions.append(
            "Keep strategy lane in paper soak until prop promotion audit is PASS/READY"
        )
    if not admin_ai_gate["ready"]:
        admin_actions = admin_ai_gate["next_actions"]
        if admin_actions:
            next_actions.append(
                "Review Jarvis/Hermes admin-AI readiness: " + str(admin_actions[0])
            )
        else:
            next_actions.append(
                "Review Jarvis/Hermes admin-AI readiness before fully unlocking VPS admin AI"
            )

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
            "trading_gate_ready": trading_gate_ready,
            "admin_ai_ready": admin_ai_gate["ready"],
            "admin_ai_status": admin_ai_gate["status"],
            "promotion_allowed": promotion_allowed,
            "order_action_allowed": False,
        },
        "runtime": {
            "services": {
                "critical": list(CRITICAL_SERVICES),
                "optional": list(OPTIONAL_SERVICES),
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
                "ibgateway": list(IBGATEWAY_TASKS),
                "missing_dashboard_durable": missing_dashboard_tasks,
                "observed": tasks,
            },
        },
        "broker_runtime": {
            "ibgateway": ibgateway_gate,
        },
        "safety_gates": {
            "broker_brackets": broker_gate,
            "promotion": promotion_gate,
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
    )


def _print_human(report: dict[str, Any]) -> None:
    summary = _as_dict(report.get("summary"))
    print(f"VPS ops hardening: {summary.get('status', 'UNKNOWN')}")
    print(f"Runtime ready: {summary.get('runtime_ready')}")
    print(f"Dashboard durable: {summary.get('dashboard_durable')}")
    print(f"Trading gate ready: {summary.get('trading_gate_ready')}")
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
