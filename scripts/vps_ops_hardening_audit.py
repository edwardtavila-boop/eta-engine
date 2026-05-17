"""Read-only VPS operations hardening audit.

This is an operator-facing health view, not a trading actuator. It can say
"the VPS/runtime is alive" while still keeping promotion blocked when broker
brackets, paper-soak, or prop gates are not clean yet.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
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
FM_STATUS_TEMPLATE_XML = workspace_roots.ETA_ENGINE_ROOT / "deploy" / "FmStatusServer.xml"
FM_STATUS_INSTALLED_XML = (
    workspace_roots.WORKSPACE_ROOT / "firm_command_center" / "services" / "FmStatusServer" / "FmStatusServer.xml"
)
FM_STATUS_INSTALLED_XML_LEGACY = workspace_roots.WORKSPACE_ROOT / "firm_command_center" / "services" / "FmStatusServer.xml"
DEFAULT_MACHINE_PYTHON = Path(r"C:\Python314\python.exe")
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
DEFAULT_ENDPOINT_READ_MAX_BYTES = 65_536
BROKER_STATE_READ_MAX_BYTES = 4_000_000
CRITICAL_SERVICE_RUNTIME_PROBES: dict[str, dict[str, Any]] = {
    "FmStatusServer": {
        "port": 8422,
        "endpoint": "local_fm_status",
    }
}
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
FORCE_MULTIPLIER_DURABLE_TASKS = (
    "ETA-ThreeAI-Sync",
)
NON_AUTHORITATIVE_TASK_ARTIFACTS: dict[str, dict[str, Any]] = {
    "ETA-SupervisorBrokerReconcile": {
        "max_age_s": 15 * 60,
        "artifacts": (
            {
                "name": "supervisor_reconcile",
                "path": workspace_roots.ETA_JARVIS_SUPERVISOR_RECONCILE_PATH,
            },
        ),
    },
    "ETA-OperatorQueueHeartbeat": {
        "max_age_s": 6 * 60 * 60,
        "artifacts": (
            {
                "name": "operator_queue_snapshot",
                "path": workspace_roots.ETA_OPERATOR_QUEUE_SNAPSHOT_PATH,
            },
        ),
    },
    "ETA-PaperLiveTransitionCheck": {
        "max_age_s": 12 * 60 * 60,
        "artifacts": (
            {
                "name": "paper_live_transition_check",
                "path": workspace_roots.ETA_RUNTIME_STATE_DIR / "paper_live_transition_check.json",
            },
        ),
    },
    "ETA-ThreeAI-Sync": {
        "max_age_s": 12 * 60 * 60,
        "artifacts": (
            {
                "name": "fm_health_snapshot",
                "path": workspace_roots.ETA_FM_HEALTH_SNAPSHOT_PATH,
            },
        ),
    },
    "ETA-PaperLive-Supervisor": {
        "max_age_s": 12 * 60 * 60,
        "artifacts": (
            {
                "name": "paper_live_transition_check",
                "path": workspace_roots.ETA_RUNTIME_STATE_DIR / "paper_live_transition_check.json",
            },
            {
                "name": "paper_live_launch_check",
                "path": workspace_roots.ETA_PAPER_LIVE_LAUNCH_CHECK_SNAPSHOT_PATH,
            },
        ),
    },
    "ETA-IndexFutures-Bar-Refresh": {
        "max_age_s": 6 * 60 * 60,
        "artifacts": (
            {
                "name": "index_futures_bar_refresh",
                "path": workspace_roots.ETA_INDEX_FUTURES_BAR_REFRESH_STATUS_PATH,
            },
            {
                "name": "symbol_intelligence_collector",
                "path": workspace_roots.ETA_SYMBOL_INTELLIGENCE_COLLECTOR_STATUS_PATH,
            },
            {
                "name": "symbol_intelligence_snapshot",
                "path": workspace_roots.ETA_SYMBOL_INTELLIGENCE_SNAPSHOT_PATH,
            },
        ),
    },
}
NON_AUTHORITATIVE_TASK_REFRESH_COMMANDS = {
    "ETA-SupervisorBrokerReconcile": (
        "run eta_engine\\deploy\\scripts\\run_supervisor_broker_reconcile_task.cmd "
        "on the VPS/Gateway-authoritative host"
    ),
    "ETA-OperatorQueueHeartbeat": "run eta_engine\\deploy\\scripts\\run_operator_queue_heartbeat_task.cmd",
    "ETA-PaperLiveTransitionCheck": "run eta_engine\\deploy\\scripts\\run_paper_live_transition_check.cmd",
    "ETA-ThreeAI-Sync": "run python -B -m eta_engine.scripts.force_multiplier_health --json-out --quiet",
    "ETA-PaperLive-Supervisor": "run eta_engine\\deploy\\scripts\\run_paper_live_transition_check.cmd",
    "ETA-IndexFutures-Bar-Refresh": "run eta_engine\\deploy\\scripts\\run_index_futures_bar_refresh_task.cmd",
}
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
BROKER_STATE_REFRESH_URL = f"{BROKER_STATE_URL}?refresh=1"
FUTURES_MONTH_CODES = frozenset("FGHJKMNQUVXZ")
DASHBOARD_SCHEMA_RELOAD_COMMAND = (
    r"scripts\reload-command-center-admin.cmd -SkipPublicCheck -SkipWatchdogRegistration"
)
ENDPOINTS = (
    {
        "name": "local_dashboard_api_diagnostics",
        "url": "http://127.0.0.1:8000/api/dashboard/diagnostics",
        "critical": True,
        "timeout_s": 15.0,
        "retries": 1,
    },
    {
        "name": "local_dashboard_proxy_diagnostics",
        "url": "http://127.0.0.1:8421/api/dashboard/diagnostics",
        "critical": True,
        "timeout_s": 15.0,
        "retries": 1,
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


def _owner_details_list(value: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    if isinstance(value, list):
        return [_as_dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _port_owner_runner_kind(owner_details: list[dict[str, Any]]) -> str:
    for item in owner_details:
        command_line = str(item.get("CommandLine") or item.get("command_line") or "")
        lowered = command_line.lower()
        if "-m eta_engine.deploy.fm_status_server" in lowered:
            return "manual_module_runner"
        if "uvicorn eta_engine.deploy.fm_status_server:app" in lowered:
            return "manual_uvicorn_runner"
    return ""


def _port_owner_runner_label(owner_details: list[dict[str, Any]]) -> str:
    kind = _port_owner_runner_kind(owner_details)
    if kind == "manual_module_runner":
        return "manual module runner"
    if kind == "manual_uvicorn_runner":
        return "manual uvicorn runner"
    return ""


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
  $ownerDetails = @($owners | ForEach-Object {{
     $proc = Get-Process -Id $_ -ErrorAction SilentlyContinue
     $procCim = Get-CimInstance Win32_Process -Filter "ProcessId=$_" -ErrorAction SilentlyContinue
     if ($null -eq $proc) {{
       [pscustomobject]@{{Pid=$_;Name=$null;Path=$null;CommandLine=$procCim.CommandLine}}
     }} else {{
       [pscustomobject]@{{Pid=$proc.Id;Name=$proc.ProcessName;Path=$proc.Path;CommandLine=$procCim.CommandLine}}
     }}
  }})
  [pscustomobject]@{{Port=$port;Listening=($null -ne $connections);Owners=$owners;OwnerDetails=$ownerDetails}}
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
            "owner_details": _owner_details_list(item.get("OwnerDetails")),
        }
    return ports


def collect_task_status() -> dict[str, dict[str, Any]]:
    """Collect expected scheduled-task status without modifying Task Scheduler."""
    names = list(
        DASHBOARD_DURABLE_TASKS
        + PAPER_LIVE_DURABLE_TASKS
        + DATA_PIPELINE_TASKS
        + FORCE_MULTIPLIER_DURABLE_TASKS
        + IBGATEWAY_TASKS
        + WATCHDOG_OBSERVED_TASKS
        + ("ETA-Autopilot",)
    )
    quoted = ", ".join(f"'{name}'" for name in names)
    command = f"""
$names = @({quoted})
$results = foreach ($name in $names) {{
  try {{
    $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
  }} catch {{
    $task = $null
  }}
  if ($null -eq $task) {{
    $schedulerProbe = ((& schtasks.exe /query /tn $name /fo LIST /v 2>&1) | Out-String).Trim()
    $state = 'Unknown'
    $probeError = $schedulerProbe
    if ($schedulerProbe -match 'Access is denied') {{
      $state = 'AccessDenied'
      $probeError = 'Access is denied.'
    }} elseif ($schedulerProbe -match 'cannot find the file specified') {{
      $state = 'Missing'
      $probeError = 'The system cannot find the file specified.'
    }} elseif (-not $schedulerProbe) {{
      $probeError = $null
    }}
    [pscustomobject]@{{
      TaskName=$name
      State=$state
      LastTaskResult=$null
      LastRunTime=$null
      NextRunTime=$null
      Actions=$null
      Error=$probeError
      QuerySource='schtasks'
    }}
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
      Error=$null
      QuerySource='Get-ScheduledTask'
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
                "error": str(item.get("Error") or ""),
                "query_source": str(item.get("QuerySource") or ""),
            }
    return tasks


def _probe_endpoint(
    url: str,
    *,
    timeout_s: float = 8.0,
    max_bytes: int = DEFAULT_ENDPOINT_READ_MAX_BYTES,
) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "eta-vps-ops-hardening"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            body = response.read(max_bytes).decode("utf-8", errors="replace")
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
    observed: dict[str, dict[str, Any]] = {}
    for endpoint in ENDPOINTS:
        attempts = max(1, int(endpoint.get("retries", 0)) + 1)
        probe: dict[str, Any] = {}
        for _ in range(attempts):
            probe = _probe_endpoint(
                str(endpoint["url"]),
                timeout_s=float(endpoint.get("timeout_s", 8.0)),
            )
            if bool(probe.get("ok")):
                break
        observed[str(endpoint["name"])] = {
            **probe,
            "url": str(endpoint["url"]),
            "critical": bool(endpoint["critical"]),
        }
    return observed


def _xml_field(path: Path, field: str) -> str | None:
    if not path.exists():
        return None
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError):
        return None
    value = root.findtext(field)
    return value.strip() if value else None


def _resolve_eta_python() -> Path | None:
    """Mirror the Force Multiplier repair script's canonical Python resolution."""
    candidates: list[Path] = []
    explicit = os.environ.get("ETA_PYTHON_EXE")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(workspace_roots.ETA_ENGINE_ROOT / ".venv" / "Scripts" / "python.exe")
    candidates.append(DEFAULT_MACHINE_PYTHON)
    which_python = shutil.which("python")
    if which_python:
        candidates.append(Path(which_python))
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        with contextlib.suppress(OSError):
            if candidate.exists():
                return candidate
    return None


def _resolve_fm_status_installed_xml() -> Path:
    if FM_STATUS_INSTALLED_XML.exists():
        return FM_STATUS_INSTALLED_XML
    return FM_STATUS_INSTALLED_XML_LEGACY


def collect_service_config_status() -> dict[str, dict[str, Any]]:
    """Compare tracked and installed FmStatusServer WinSW XML."""
    expected = FM_STATUS_TEMPLATE_XML
    installed = _resolve_fm_status_installed_xml()
    template_executable = _xml_field(expected, "executable")
    resolved_python = _resolve_eta_python()
    expected_executable = str(resolved_python) if resolved_python else template_executable
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
            "template_executable": template_executable,
            "expected_executable": expected_executable,
            "installed_executable": installed_executable,
            "expected_executable_source": "resolved_python" if resolved_python else "template_xml",
            "installed_xml_source": "service_sidecar" if installed == FM_STATUS_INSTALLED_XML else "legacy_flat_xml",
            "expected_arguments": expected_arguments,
            "installed_arguments": installed_arguments,
        }
    }


def _artifact_file_status(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "present": False, "age_s": None, "modified_at": None}
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError as exc:
        return {
            "path": str(path),
            "present": False,
            "age_s": None,
            "modified_at": None,
            "error": str(exc),
        }
    age_s = max(0.0, ((now or datetime.now(UTC)) - modified).total_seconds())
    return {
        "path": str(path),
        "present": True,
        "age_s": round(age_s, 1),
        "modified_at": modified.isoformat(),
    }


def _live_broker_state_refresh_coverage(
    live_broker_state: dict[str, Any] | None,
    *,
    now: datetime,
) -> dict[str, Any] | None:
    payload = live_broker_state if isinstance(live_broker_state, dict) else {}
    if not payload:
        return None
    snapshot_state = str(payload.get("broker_snapshot_state") or "").strip().lower()
    source = str(payload.get("source") or "").strip()
    server_ts = payload.get("server_ts")
    age_s = _float_value(payload.get("broker_snapshot_age_s"))
    modified_at: str | None = None
    if isinstance(server_ts, (int, float)):
        observed = datetime.fromtimestamp(float(server_ts), tz=UTC)
        modified_at = observed.isoformat()
        age_s = max(0.0, (now - observed).total_seconds())
    if age_s is None:
        age_s = 0.0
    if not snapshot_state and not source and not bool(payload.get("close_history")):
        return None
    max_age_s = 15 * 60
    fresh_state = snapshot_state in {"", "fresh", "ready", "live"}
    stale = bool(age_s > max_age_s or not fresh_state)
    return {
        "task_name": "ETA-BrokerStateRefreshHeartbeat",
        "covered": True,
        "stale": stale,
        "status": snapshot_state or ("stale" if stale else "fresh"),
        "max_age_s": max_age_s,
        "path": BROKER_STATE_REFRESH_URL,
        "source": source or "live_broker_state_endpoint",
        "modified_at": modified_at,
        "age_s": round(age_s, 1),
        "artifacts": [
            {
                "name": "live_broker_state_endpoint",
                "path": BROKER_STATE_REFRESH_URL,
                "present": True,
                "age_s": round(age_s, 1),
                "modified_at": modified_at,
            }
        ],
        "broker_snapshot_state": snapshot_state,
        "broker_ready": bool(payload.get("ready")),
    }


def collect_non_authoritative_task_artifacts(
    *,
    now: datetime | None = None,
    live_broker_state: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Capture cached local artifacts that can truthfully back non-authoritative watch surfaces."""
    observed_now = now or datetime.now(UTC)
    coverage: dict[str, dict[str, Any]] = {}
    for task_name, spec in NON_AUTHORITATIVE_TASK_ARTIFACTS.items():
        max_age_s = int(spec.get("max_age_s") or 0)
        candidates: list[dict[str, Any]] = []
        selected: dict[str, Any] | None = None
        for raw_artifact in spec.get("artifacts") or ():
            artifact = _as_dict(raw_artifact)
            path_value = artifact.get("path")
            if not isinstance(path_value, Path):
                continue
            candidate = _artifact_file_status(path_value, now=observed_now)
            candidate["name"] = str(artifact.get("name") or path_value.name)
            candidates.append(candidate)
            if selected is None and candidate.get("present"):
                selected = candidate
        coverage_entry: dict[str, Any] = {
            "task_name": task_name,
            "covered": False,
            "stale": False,
            "status": "missing",
            "max_age_s": max_age_s,
            "artifacts": candidates,
        }
        if selected is not None:
            age_s = float(selected.get("age_s") or 0.0)
            stale = bool(max_age_s and age_s > max_age_s)
            coverage_entry.update(
                {
                    "covered": True,
                    "stale": stale,
                    "status": "stale" if stale else "fresh",
                    "path": selected.get("path"),
                    "source": selected.get("name"),
                    "modified_at": selected.get("modified_at"),
                    "age_s": selected.get("age_s"),
                }
            )
        coverage[task_name] = coverage_entry
    broker_refresh_coverage = _live_broker_state_refresh_coverage(live_broker_state, now=observed_now)
    if broker_refresh_coverage is not None:
        coverage["ETA-BrokerStateRefreshHeartbeat"] = broker_refresh_coverage
    return coverage


def _critical_services_not_running(services: dict[str, dict[str, Any]]) -> list[str]:
    return [
        name for name in CRITICAL_SERVICES if str(_as_dict(services.get(name)).get("status") or "").lower() != "running"
    ]


def _service_runtime_drift(
    services: dict[str, dict[str, Any]],
    ports: dict[int, dict[str, Any]],
    endpoints: dict[str, dict[str, Any]],
) -> list[str]:
    return list(_service_runtime_drift_detail(services, ports, endpoints))


def _service_runtime_drift_detail(
    services: dict[str, dict[str, Any]],
    ports: dict[int, dict[str, Any]],
    endpoints: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    detail: dict[str, dict[str, Any]] = {}
    for name in _critical_services_not_running(services):
        probe = _as_dict(CRITICAL_SERVICE_RUNTIME_PROBES.get(name))
        port = probe.get("port")
        endpoint_name = str(probe.get("endpoint") or "")
        if not isinstance(port, int) or not endpoint_name:
            continue
        port_status = _as_dict(ports.get(port))
        endpoint_status = _as_dict(endpoints.get(endpoint_name))
        service_status = _as_dict(services.get(name))
        port_ok = bool(port_status.get("listening"))
        endpoint_ok = bool(endpoint_status.get("ok"))
        if port_ok and endpoint_ok:
            owner_details = _owner_details_list(port_status.get("owner_details"))
            runner_label = _port_owner_runner_label(owner_details)
            reason = "critical endpoint is alive while the supervised Windows service is not running"
            if runner_label:
                reason = (
                    "critical endpoint is alive via a "
                    + runner_label
                    + " while the supervised Windows service is not running"
                )
            detail[name] = {
                "service_status": service_status.get("status"),
                "service_start_type": service_status.get("start_type"),
                "port": port,
                "port_listening": port_ok,
                "port_owners": _as_list(port_status.get("owners")),
                "port_owner_details": owner_details,
                "port_owner_runner": _port_owner_runner_kind(owner_details),
                "port_owner_runner_label": runner_label,
                "endpoint": endpoint_name,
                "endpoint_ok": endpoint_ok,
                "reason": reason,
            }
    return detail


def _missing_ports(ports: dict[int, dict[str, Any]]) -> list[int]:
    return [port for port in REQUIRED_PORTS if not bool(_as_dict(ports.get(port)).get("listening"))]


def _tasks_with_state(task_names: tuple[str, ...], tasks: dict[str, dict[str, Any]], state: str) -> list[str]:
    expected = state.lower()
    return [name for name in task_names if str(_as_dict(tasks.get(name)).get("state") or "").lower() == expected]


def _missing_dashboard_tasks(tasks: dict[str, dict[str, Any]]) -> list[str]:
    return _tasks_with_state(DASHBOARD_DURABLE_TASKS, tasks, "Missing")


def _missing_paper_live_tasks(tasks: dict[str, dict[str, Any]]) -> list[str]:
    return _tasks_with_state(PAPER_LIVE_DURABLE_TASKS, tasks, "Missing")


def _missing_data_pipeline_tasks(tasks: dict[str, dict[str, Any]]) -> list[str]:
    return _tasks_with_state(DATA_PIPELINE_TASKS, tasks, "Missing")


def _missing_force_multiplier_tasks(tasks: dict[str, dict[str, Any]]) -> list[str]:
    return _tasks_with_state(FORCE_MULTIPLIER_DURABLE_TASKS, tasks, "Missing")


def _access_denied_dashboard_tasks(tasks: dict[str, dict[str, Any]]) -> list[str]:
    return _tasks_with_state(DASHBOARD_DURABLE_TASKS, tasks, "AccessDenied")


def _access_denied_paper_live_tasks(tasks: dict[str, dict[str, Any]]) -> list[str]:
    return _tasks_with_state(PAPER_LIVE_DURABLE_TASKS, tasks, "AccessDenied")


def _access_denied_data_pipeline_tasks(tasks: dict[str, dict[str, Any]]) -> list[str]:
    return _tasks_with_state(DATA_PIPELINE_TASKS, tasks, "AccessDenied")


def _access_denied_force_multiplier_tasks(tasks: dict[str, dict[str, Any]]) -> list[str]:
    return _tasks_with_state(FORCE_MULTIPLIER_DURABLE_TASKS, tasks, "AccessDenied")


def _artifact_backed_missing_tasks(
    missing_tasks: list[str],
    task_artifacts: dict[str, dict[str, Any]],
) -> list[str]:
    return [name for name in missing_tasks if bool(_as_dict(task_artifacts.get(name)).get("covered"))]


def _stale_artifact_backed_tasks(
    task_names: list[str],
    task_artifacts: dict[str, dict[str, Any]],
) -> list[str]:
    return [name for name in task_names if bool(_as_dict(task_artifacts.get(name)).get("stale"))]


def _artifact_refresh_guidance(task_names: list[str]) -> str:
    commands = [
        command
        for name in task_names
        if (command := NON_AUTHORITATIVE_TASK_REFRESH_COMMANDS.get(name))
    ]
    if not commands:
        return ""
    return "; " + "; ".join(dict.fromkeys(commands))


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
        hardening = _as_dict(payload.get("vps_ops_hardening"))
        if (
            "command_center_watchdog" not in payload
            or "eta_readiness_snapshot" not in payload
            or "daily_stop_reset_audit" not in payload
            or "hardening" not in payload
            or payload.get("hardening") != payload.get("vps_ops_hardening")
            or checks.get("command_center_watchdog_contract") is not True
            or checks.get("eta_readiness_snapshot_contract") is not True
            or checks.get("daily_stop_reset_audit_contract") is not True
            or checks.get("hardening_contract") is not True
            or checks.get("vps_ops_hardening_contract") is not True
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
    stale_flat_symbols = [
        str(symbol)
        for symbol in (
            _as_list(summary.get("stale_flat_open_order_symbols"))
            or _as_list(position_summary.get("stale_flat_open_order_symbols"))
        )
        if str(symbol)
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
        "stale_flat_open_order_count": int(
            summary.get("stale_flat_open_order_count")
            or position_summary.get("stale_flat_open_order_count")
            or 0
        ),
        "stale_flat_open_order_symbols": stale_flat_symbols,
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


def _replay_bar_label(symbol: str) -> str:
    clean = str(symbol or "").strip().upper()
    if clean:
        return f"{clean} 5-minute replay bars"
    return "5-minute replay bars"


def _runner_shadow_bar_gap_action(label: str, symbol: str, outcome_evidence: dict[str, Any]) -> str:
    bar_label = _replay_bar_label(symbol)
    no_bar_after_signal = int(outcome_evidence.get("no_bar_after_signal") or 0)
    missing_bar_datasets = int(outcome_evidence.get("missing_bar_datasets") or 0)
    coverage_end = str(outcome_evidence.get("latest_bar_coverage_end_ts") or "").strip()
    if no_bar_after_signal > 0:
        if coverage_end:
            return (
                f"Refresh {bar_label} for runner-up {label}; latest available replay bar is {coverage_end} "
                f"and {no_bar_after_signal} shadow signals arrived after coverage ended"
            )
        return (
            f"Refresh {bar_label} for runner-up {label}; {no_bar_after_signal} shadow signals arrived after "
            "available replay coverage ended"
        )
    if missing_bar_datasets > 0:
        return (
            f"Repair replay bar sourcing for runner-up {label}; no {bar_label} are available for "
            f"{missing_bar_datasets} shadow signals"
        )
    missing_bars = int(outcome_evidence.get("missing_bars") or 0)
    return (
        f"Repair shadow outcome replay for runner-up {label}; {missing_bars} shadow signals "
        "cannot replay into outcomes"
    )


def _promotion_gate_action(gate: dict[str, Any]) -> str:
    runner = _as_dict(gate.get("next_runner_candidate"))
    runner_id = str(runner.get("bot_id") or "").strip()
    runner_symbol = str(runner.get("symbol") or "").strip()
    if runner_id:
        next_action = str(runner.get("next_action") or "").strip()
        if next_action:
            return next_action
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
                    return _runner_shadow_bar_gap_action(label, runner_symbol, outcome_evidence)
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
        runner_required = next((item for item in required if "runner-up candidate" in item), "")
        return "Keep strategy lane in paper soak: " + (runner_required or required[0])
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
            "non_authoritative_host": False,
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
    if not isinstance(live_broker_state, dict):
        return None
    broker_state_source = str(live_broker_state.get("source") or "").strip()
    broker_snapshot_state = str(live_broker_state.get("broker_snapshot_state") or "").strip().lower()
    fresh_non_authoritative_zero_position_truth = (
        broker_state_source == "live_broker_rest" and broker_snapshot_state == "fresh"
    )
    if not live_broker_state.get("ready") and not fresh_non_authoritative_zero_position_truth:
        return None
    broker_by_root = _broker_positions_by_root(live_broker_state)
    supervisor_by_root = _supervisor_positions_by_root(supervisor_heartbeat)
    ibkr_state = _as_dict(live_broker_state.get("ibkr"))
    reported_open_count = _float_value(live_broker_state.get("open_position_count"))
    if reported_open_count is None:
        reported_open_count = _float_value(ibkr_state.get("open_position_count"))
    if not broker_by_root and not supervisor_by_root:
        if reported_open_count is None:
            return None
        if reported_open_count > 0:
            return None
    if not broker_by_root and reported_open_count is not None and reported_open_count > 0:
        return None
    if not broker_by_root and supervisor_by_root and reported_open_count is None:
        return None
    snapshot = _diff_position_books(
        broker_by_root=broker_by_root,
        supervisor_by_root=supervisor_by_root,
        checked_at=datetime.now(UTC).isoformat(),
        source="supervisor_heartbeat_and_live_broker_state",
        brokers_queried=["ibkr"]
        if (
            _as_dict((live_broker_state or {}).get("ibkr")).get("ready")
            or fresh_non_authoritative_zero_position_truth
        )
        else [],
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


def _current_supervisor_reconcile_coverage(gate: dict[str, Any]) -> dict[str, Any] | None:
    source = str(gate.get("source") or "").strip()
    if source != "supervisor_heartbeat_and_live_broker_state":
        return None
    checked_at = str(gate.get("checked_at") or "").strip() or None
    age_s = _float_value(gate.get("age_s"))
    max_age_s = int(gate.get("max_age_s") or SUPERVISOR_RECONCILE_MAX_AGE_S)
    return {
        "task_name": "ETA-SupervisorBrokerReconcile",
        "covered": True,
        "stale": False,
        "status": "fresh",
        "max_age_s": max_age_s,
        "path": "",
        "source": source,
        "modified_at": checked_at,
        "age_s": round(age_s, 1) if age_s is not None else None,
        "artifacts": [
            {
                "name": "supervisor_heartbeat_and_live_broker_state",
                "path": "",
                "present": True,
                "age_s": round(age_s, 1) if age_s is not None else None,
                "modified_at": checked_at,
            }
        ],
        "heartbeat_ts": gate.get("heartbeat_ts"),
        "broker_state_source": gate.get("broker_state_source"),
        "brokers_queried": [str(item) for item in _as_list(gate.get("brokers_queried")) if str(item)],
    }


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
    raw_blocking_count = supervisor_reconcile.get("blocking_mismatch_count")
    blocking_mismatch_count = (
        int(raw_blocking_count)
        if raw_blocking_count is not None
        else len(broker_only) + len(divergent)
    )

    if artifact_status:
        status = f"{artifact_status}_reconcile_snapshot"
        ready = False
    elif age_s is None or age_s > SUPERVISOR_RECONCILE_MAX_AGE_S:
        status = "STALE_RECONCILE_SNAPSHOT"
        ready = False
    elif blocking_mismatch_count:
        status = "BLOCKED_BROKER_SUPERVISOR_RECONCILE"
        ready = False
    elif supervisor_only:
        status = "PASS_SUPERVISOR_ONLY_LOCAL_PAPER"
        ready = True
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
        "blocking_mismatch_count": blocking_mismatch_count,
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
    non_authoritative_task_artifacts: dict[str, dict[str, Any]] | None = None,
    jarvis_hermes_admin: dict[str, Any] | None = None,
    supervisor_reconcile: dict[str, Any] | None = None,
    supervisor_heartbeat: dict[str, Any] | None = None,
    live_broker_state: dict[str, Any] | None = None,
    repo_revision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the deterministic hardening report from collected inputs."""
    tasks = tasks or {}
    non_authoritative_task_artifacts = {
        str(name): dict(_as_dict(entry))
        for name, entry in (non_authoritative_task_artifacts or {}).items()
    }
    service_runtime_drift_detail = _service_runtime_drift_detail(services, ports, endpoints)
    service_runtime_drift = list(service_runtime_drift_detail)
    service_down = [name for name in _critical_services_not_running(services) if name not in service_runtime_drift]
    missing_ports = _missing_ports(ports)
    missing_dashboard_tasks = _missing_dashboard_tasks(tasks)
    observed_missing_dashboard_tasks = list(missing_dashboard_tasks)
    observed_missing_paper_live_tasks = _missing_paper_live_tasks(tasks)
    observed_missing_data_pipeline_tasks = _missing_data_pipeline_tasks(tasks)
    observed_missing_force_multiplier_tasks = _missing_force_multiplier_tasks(tasks)
    access_denied_dashboard_tasks = _access_denied_dashboard_tasks(tasks)
    access_denied_paper_live_tasks = _access_denied_paper_live_tasks(tasks)
    access_denied_data_pipeline_tasks = _access_denied_data_pipeline_tasks(tasks)
    access_denied_force_multiplier_tasks = _access_denied_force_multiplier_tasks(tasks)
    stale_restart_hooks = _stale_supervisor_restart_hooks(tasks)
    endpoint_failures = _critical_endpoint_failures(endpoints)
    dashboard_schema_drift = _dashboard_schema_drift(endpoints)
    drifted_configs = _config_drift(service_config)
    broker_gate = _broker_gate_summary(broker_bracket_audit)
    promotion_gate = _promotion_gate_summary(promotion_audit)
    ibgateway_gate = _ibgateway_summary(ibgateway_reauth, ports)
    artifact_backed_dashboard_tasks: list[str] = []
    artifact_backed_paper_live_tasks: list[str] = []
    artifact_backed_data_pipeline_tasks: list[str] = []
    artifact_backed_force_multiplier_tasks: list[str] = []
    stale_artifact_backed_dashboard_tasks: list[str] = []
    stale_artifact_backed_paper_live_tasks: list[str] = []
    stale_artifact_backed_data_pipeline_tasks: list[str] = []
    stale_artifact_backed_force_multiplier_tasks: list[str] = []
    if ibgateway_gate["non_authoritative_host"]:
        artifact_backed_dashboard_tasks = _artifact_backed_missing_tasks(
            observed_missing_dashboard_tasks,
            non_authoritative_task_artifacts,
        )
        artifact_backed_paper_live_tasks = _artifact_backed_missing_tasks(
            observed_missing_paper_live_tasks,
            non_authoritative_task_artifacts,
        )
        artifact_backed_data_pipeline_tasks = _artifact_backed_missing_tasks(
            observed_missing_data_pipeline_tasks,
            non_authoritative_task_artifacts,
        )
        artifact_backed_force_multiplier_tasks = _artifact_backed_missing_tasks(
            observed_missing_force_multiplier_tasks,
            non_authoritative_task_artifacts,
        )
        stale_artifact_backed_dashboard_tasks = _stale_artifact_backed_tasks(
            artifact_backed_dashboard_tasks,
            non_authoritative_task_artifacts,
        )
        stale_artifact_backed_paper_live_tasks = _stale_artifact_backed_tasks(
            artifact_backed_paper_live_tasks,
            non_authoritative_task_artifacts,
        )
        stale_artifact_backed_data_pipeline_tasks = _stale_artifact_backed_tasks(
            artifact_backed_data_pipeline_tasks,
            non_authoritative_task_artifacts,
        )
        stale_artifact_backed_force_multiplier_tasks = _stale_artifact_backed_tasks(
            artifact_backed_force_multiplier_tasks,
            non_authoritative_task_artifacts,
        )
    missing_dashboard_tasks = [
        name for name in observed_missing_dashboard_tasks if name not in artifact_backed_dashboard_tasks
    ]
    missing_paper_live_tasks = [
        name for name in observed_missing_paper_live_tasks if name not in artifact_backed_paper_live_tasks
    ]
    missing_data_pipeline_tasks = [
        name for name in observed_missing_data_pipeline_tasks if name not in artifact_backed_data_pipeline_tasks
    ]
    missing_force_multiplier_tasks = [
        name
        for name in observed_missing_force_multiplier_tasks
        if name not in artifact_backed_force_multiplier_tasks
    ]
    admin_ai_gate = _jarvis_hermes_admin_summary(jarvis_hermes_admin)
    effective_supervisor_reconcile = _effective_supervisor_reconcile(
        supervisor_reconcile=supervisor_reconcile,
        supervisor_heartbeat=supervisor_heartbeat,
        live_broker_state=live_broker_state,
    )
    supervisor_reconcile_gate = _supervisor_reconcile_summary(effective_supervisor_reconcile)
    current_supervisor_reconcile_coverage = _current_supervisor_reconcile_coverage(supervisor_reconcile_gate)
    if current_supervisor_reconcile_coverage is not None:
        non_authoritative_task_artifacts["ETA-SupervisorBrokerReconcile"] = current_supervisor_reconcile_coverage
    if (
        supervisor_reconcile_gate.get("source") == "supervisor_heartbeat_and_live_broker_state"
        and "ETA-SupervisorBrokerReconcile" in stale_artifact_backed_dashboard_tasks
    ):
        stale_artifact_backed_dashboard_tasks = [
            name for name in stale_artifact_backed_dashboard_tasks if name != "ETA-SupervisorBrokerReconcile"
        ]
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
    force_multiplier_durable = not missing_force_multiplier_tasks
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
    for name in service_runtime_drift:
        drift = _as_dict(service_runtime_drift_detail.get(name))
        owner_details = _owner_details_list(drift.get("port_owner_details"))
        owner_names = ", ".join(
            str(item.get("Name") or item.get("name") or item.get("Pid") or item.get("pid"))
            for item in owner_details[:3]
        )
        runner_label = str(drift.get("port_owner_runner_label") or "").strip()
        runner_hint = f", {runner_label}" if runner_label else ""
        owner_hint = f" (port {drift.get('port')} owner={owner_names}{runner_hint})" if owner_names else ""
        next_actions.append(
            "Repair supervised Windows service ownership for live endpoint: "
            f"{name}{owner_hint}; run eta_engine\\deploy\\scripts\\repair_force_multiplier_control_plane_admin.cmd "
            "/RestartService"
        )
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
    if stale_artifact_backed_dashboard_tasks:
        next_actions.append(
            "Refresh local non-authoritative dashboard watch artifacts; cached authoritative coverage is stale for "
            + ", ".join(stale_artifact_backed_dashboard_tasks)
            + _artifact_refresh_guidance(stale_artifact_backed_dashboard_tasks)
        )
    if missing_paper_live_tasks:
        next_actions.append(
            "Repair paper-live scheduled task lane: " + ", ".join(missing_paper_live_tasks)
        )
    if missing_data_pipeline_tasks:
        next_actions.append(
            "Repair data-pipeline scheduled task lane: " + ", ".join(missing_data_pipeline_tasks)
        )
    if missing_force_multiplier_tasks:
        next_actions.append(
            "Repair Force Multiplier scheduled task lane: "
            + ", ".join(missing_force_multiplier_tasks)
            + "; run eta_engine\\deploy\\scripts\\repair_force_multiplier_control_plane_admin.cmd /RestartService"
        )
    if stale_artifact_backed_force_multiplier_tasks:
        next_actions.append(
            "Refresh local non-authoritative Force Multiplier watch artifacts; cached authoritative coverage is stale for "
            + ", ".join(stale_artifact_backed_force_multiplier_tasks)
            + _artifact_refresh_guidance(stale_artifact_backed_force_multiplier_tasks)
        )
    if stale_artifact_backed_paper_live_tasks:
        next_actions.append(
            "Refresh local non-authoritative paper-live watch artifacts; cached authoritative coverage is stale for "
            + ", ".join(stale_artifact_backed_paper_live_tasks)
            + _artifact_refresh_guidance(stale_artifact_backed_paper_live_tasks)
        )
    if stale_artifact_backed_data_pipeline_tasks:
        next_actions.append(
            "Refresh local non-authoritative data-pipeline watch artifacts; cached authoritative coverage is stale for "
            + ", ".join(stale_artifact_backed_data_pipeline_tasks)
            + _artifact_refresh_guidance(stale_artifact_backed_data_pipeline_tasks)
        )
    if stale_restart_hooks:
        next_actions.append(
            "Disable or repair stale supervisor restart hook(s): "
            + ", ".join(stale_restart_hooks)
            + f"; target {CANONICAL_SUPERVISOR_TASK} only"
        )
    if dashboard_schema_drift:
        next_actions.append(
            "Reload dashboard API/proxy so live diagnostics schema includes the compatibility aliases and audit contracts: "
            + ", ".join(dashboard_schema_drift)
            + f"; run {DASHBOARD_SCHEMA_RELOAD_COMMAND}"
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
    stale_flat_symbols = ", ".join(broker_gate["stale_flat_open_order_symbols"])
    if stale_flat_symbols:
        next_actions.append(f"Cancel stale broker open orders for {stale_flat_symbols} before clearing paper-live")
    elif broker_gate["stale_flat_open_order_count"]:
        next_actions.append(
            "Cancel stale broker open orders for "
            f"{broker_gate['stale_flat_open_order_count']} flat-symbol order(s) before clearing paper-live"
        )
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
    elif drifted_configs or service_runtime_drift or (dashboard_schema_drift and trading_gate_ready):
        status = "YELLOW_RESTART_REQUIRED"
    elif not (dashboard_durable and force_multiplier_durable) and trading_gate_ready:
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
            "service_down": service_down,
            "service_runtime_drift": service_runtime_drift,
            "service_config_drift": drifted_configs,
            "missing_ports": missing_ports,
            "critical_endpoint_failures": endpoint_failures,
            "dashboard_durable": dashboard_durable,
            "force_multiplier_durable": force_multiplier_durable,
            "missing_dashboard_durable": missing_dashboard_tasks,
            "missing_force_multiplier_durable": missing_force_multiplier_tasks,
            "missing_paper_live_durable": missing_paper_live_tasks,
            "missing_data_pipeline": missing_data_pipeline_tasks,
            "stale_artifact_backed_dashboard_durable": stale_artifact_backed_dashboard_tasks,
            "stale_artifact_backed_force_multiplier_durable": stale_artifact_backed_force_multiplier_tasks,
            "stale_artifact_backed_paper_live_durable": stale_artifact_backed_paper_live_tasks,
            "stale_artifact_backed_data_pipeline": stale_artifact_backed_data_pipeline_tasks,
            "dashboard_schema_current": not dashboard_schema_drift,
            "dashboard_schema_drift": dashboard_schema_drift,
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
                "runtime_drift": service_runtime_drift,
                "runtime_drift_detail": service_runtime_drift_detail,
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
                "force_multiplier_durable": list(FORCE_MULTIPLIER_DURABLE_TASKS),
                "ibgateway": list(IBGATEWAY_TASKS),
                "watchdog_observed": list(WATCHDOG_OBSERVED_TASKS),
                "observed_missing_dashboard_durable": observed_missing_dashboard_tasks,
                "missing_dashboard_durable": missing_dashboard_tasks,
                "artifact_backed_missing_dashboard_durable": artifact_backed_dashboard_tasks,
                "stale_artifact_backed_dashboard_durable": stale_artifact_backed_dashboard_tasks,
                "observed_missing_paper_live_durable": observed_missing_paper_live_tasks,
                "missing_paper_live_durable": missing_paper_live_tasks,
                "artifact_backed_missing_paper_live_durable": artifact_backed_paper_live_tasks,
                "stale_artifact_backed_paper_live_durable": stale_artifact_backed_paper_live_tasks,
                "observed_missing_data_pipeline": observed_missing_data_pipeline_tasks,
                "missing_data_pipeline": missing_data_pipeline_tasks,
                "observed_missing_force_multiplier_durable": observed_missing_force_multiplier_tasks,
                "missing_force_multiplier_durable": missing_force_multiplier_tasks,
                "artifact_backed_missing_force_multiplier_durable": artifact_backed_force_multiplier_tasks,
                "stale_artifact_backed_force_multiplier_durable": stale_artifact_backed_force_multiplier_tasks,
                "artifact_backed_missing_data_pipeline": artifact_backed_data_pipeline_tasks,
                "stale_artifact_backed_data_pipeline": stale_artifact_backed_data_pipeline_tasks,
                "access_denied_dashboard_durable": access_denied_dashboard_tasks,
                "access_denied_paper_live_durable": access_denied_paper_live_tasks,
                "access_denied_data_pipeline": access_denied_data_pipeline_tasks,
                "access_denied_force_multiplier_durable": access_denied_force_multiplier_tasks,
                "stale_supervisor_restart_hooks": stale_restart_hooks,
                "non_authoritative_task_artifacts": non_authoritative_task_artifacts,
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
    broker_state_probe = _probe_endpoint(
        BROKER_STATE_URL,
        timeout_s=8.0,
        max_bytes=BROKER_STATE_READ_MAX_BYTES,
    )
    live_broker_state = _as_dict(broker_state_probe.get("payload"))
    broker_state_refresh_probe = _probe_endpoint(
        BROKER_STATE_REFRESH_URL,
        timeout_s=8.0,
        max_bytes=BROKER_STATE_READ_MAX_BYTES,
    )
    live_broker_refresh_state = _as_dict(broker_state_refresh_probe.get("payload")) or live_broker_state
    return build_report(
        services=collect_service_status(),
        ports=collect_port_status(),
        endpoints=collect_endpoint_status(),
        broker_bracket_audit=_read_json(workspace_roots.ETA_BROKER_BRACKET_AUDIT_PATH),
        promotion_audit=_read_json(workspace_roots.ETA_PROP_STRATEGY_PROMOTION_AUDIT_PATH),
        service_config=collect_service_config_status(),
        tasks=collect_task_status(),
        ibgateway_reauth=_read_json(workspace_roots.ETA_RUNTIME_STATE_DIR / "ibgateway_reauth.json"),
        non_authoritative_task_artifacts=collect_non_authoritative_task_artifacts(
            live_broker_state=live_broker_refresh_state,
        ),
        jarvis_hermes_admin=collect_jarvis_hermes_admin_status(),
        supervisor_reconcile=_read_json(workspace_roots.ETA_JARVIS_SUPERVISOR_RECONCILE_PATH),
        supervisor_heartbeat=_read_json(workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH),
        live_broker_state=live_broker_refresh_state,
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
    parser.add_argument(
        "--write-latest",
        action="store_true",
        help="alias for --json-out using the canonical var/eta_engine/state path",
    )
    args = parser.parse_args(argv)

    report = collect_live_report()
    json_out = str(DEFAULT_OUT) if args.write_latest and not args.json_out else args.json_out
    if json_out:
        out = Path(json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)
    return 1 if str(_as_dict(report.get("summary")).get("status")).startswith("RED") else 0


if __name__ == "__main__":
    raise SystemExit(main())
