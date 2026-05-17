"""Public edge route watchdog for the ETA dashboard.

The public edge (`FirmCommandCenterEdge`) should serve the same dashboard truth
as the canonical operator bridge on ``127.0.0.1:8421``. When the edge drifts
back to the legacy ``8420`` lane, the website can stay up while showing stale
or schema-old data. This watchdog detects that drift, normalizes the Caddy
reverse proxy target back to ``127.0.0.1:8421``, restarts only the edge
service, and records a heartbeat under ``var/eta_engine/state``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402
from eta_engine.scripts.uptime_events import record_uptime_event  # noqa: E402

logger = logging.getLogger("public_edge_route_watchdog")

EXPECTED_SUMMARY_FIELDS = (
    "active_bots",
    "runtime_active_bots",
    "running_bots",
    "live_attached_bots",
    "live_in_trade_bots",
    "idle_live_bots",
    "inactive_runtime_bots",
    "staged_bots",
    "truth_status",
)
REVERSE_PROXY_RE = re.compile(r"(?im)^(\s*reverse_proxy\s+)([^\s{]+)(.*)$")
DEFAULT_PUBLIC_HOSTNAME = os.getenv("ETA_PUBLIC_EDGE_HOSTNAME", "ops.evolutionarytradingalgo.com")
DEFAULT_TIMEOUT_S = float(os.getenv("ETA_PUBLIC_EDGE_TIMEOUT_S", "20"))
DEFAULT_RESTART_DELAY_S = float(os.getenv("ETA_PUBLIC_EDGE_RESTART_DELAY_S", "3"))
DEFAULT_SERVICE_NAME = os.getenv("ETA_PUBLIC_EDGE_SERVICE_NAME", "FirmCommandCenterEdge")
DEFAULT_CADDYFILE_PATH = Path(
    os.getenv("ETA_PUBLIC_EDGE_CADDYFILE", "").strip()
    or workspace_roots.WORKSPACE_ROOT
    / "firm_command_center"
    / "services"
    / "FirmCommandCenter.Caddyfile"
)
DEFAULT_CADDY_EXE = Path(
    os.getenv("ETA_PUBLIC_EDGE_CADDY_EXE", "").strip()
    or workspace_roots.WORKSPACE_ROOT / "firm_command_center" / "services" / "caddy.exe"
)
DEFAULT_CLOUDFLARED_CONFIG_PATH = Path(
    os.getenv("ETA_PUBLIC_EDGE_CLOUDFLARED_CONFIG", "").strip()
    or workspace_roots.WORKSPACE_ROOT / "var" / "cloudflare" / "eta-engine-cloudflared.yml"
)
DEFAULT_CLOUDFLARED_SERVICE_NAME = os.getenv("ETA_PUBLIC_EDGE_CLOUDFLARED_SERVICE_NAME", "Cloudflared")
DEFAULT_DIRECT_TARGET = os.getenv("ETA_PUBLIC_EDGE_DIRECT_TARGET", "127.0.0.1:8000")
DEFAULT_HEARTBEAT_PATH = (
    workspace_roots.ETA_RUNTIME_STATE_DIR / "public_edge_route_watchdog_heartbeat.json"
)


def _normalize_service_target(raw_value: str | None) -> str | None:
    """Normalize a route target to ``host:port`` when possible."""
    value = str(raw_value or "").strip().strip('"').strip("'")
    if not value or value.startswith("http_status:"):
        return None
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme and parsed.hostname:
        if parsed.port is not None:
            return f"{parsed.hostname}:{parsed.port}"
        if parsed.scheme == "https":
            return f"{parsed.hostname}:443"
        if parsed.scheme == "http":
            return f"{parsed.hostname}:80"
    if "://" not in value:
        return value
    return None


def _service_url_from_target(target: str, *, path: str = "/api/bot-fleet") -> str:
    """Convert ``host:port`` into a local HTTP probe URL."""
    return f"http://{target}{path}"


def _cloudflare_mode_active(
    *,
    caddyfile_path: Path = DEFAULT_CADDYFILE_PATH,
    cloudflared_config_path: Path = DEFAULT_CLOUDFLARED_CONFIG_PATH,
    public_hostname: str = DEFAULT_PUBLIC_HOSTNAME,
) -> bool:
    target, reason = read_cloudflare_ingress_target(cloudflared_config_path, public_hostname)
    return target is not None and reason == "ok"


@dataclass(slots=True)
class EndpointProbe:
    """Probe result for one dashboard surface."""

    healthy: bool
    url: str
    status_code: int | None
    reason: str
    elapsed_ms: int
    summary: dict[str, object]
    truth_summary_line: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class RepairResult:
    """Repair details for one public-edge route normalization attempt."""

    ok: bool
    changed_caddyfile: bool
    previous_target: str | None
    current_target: str | None
    restart_ok: bool
    reason: str
    backup_path: str | None = None
    validation_reason: str | None = None
    restart_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class RouteWatchdogDecision:
    """Structured output for one watchdog tick."""

    checked_at: str
    action: str
    route_ok_before: bool
    route_ok_after: bool | None
    expected_target: str
    target_before: str | None
    target_after: str | None
    public_probe: EndpointProbe
    canonical_probe: EndpointProbe
    mismatch_reasons: list[str]
    repair: RepairResult | None = None
    post_public_probe: EndpointProbe | None = None
    post_canonical_probe: EndpointProbe | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["public_probe"] = self.public_probe.to_dict()
        payload["canonical_probe"] = self.canonical_probe.to_dict()
        if self.repair is not None:
            payload["repair"] = self.repair.to_dict()
        if self.post_public_probe is not None:
            payload["post_public_probe"] = self.post_public_probe.to_dict()
        if self.post_canonical_probe is not None:
            payload["post_canonical_probe"] = self.post_canonical_probe.to_dict()
        return payload


def _summary_view(payload: dict[str, Any]) -> dict[str, object]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {field: summary.get(field) for field in EXPECTED_SUMMARY_FIELDS}


def probe_endpoint(url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> EndpointProbe:
    """Fetch one dashboard endpoint and normalize a comparable summary view."""
    started = time.monotonic()
    status_code: int | None = None
    try:
        request = urllib.request.Request(url, headers={"Cache-Control": "no-store"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            status_code = int(response.status)
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return EndpointProbe(
            healthy=False,
            url=url,
            status_code=int(exc.code),
            reason=f"http_error:{int(exc.code)}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            summary={},
        )
    except json.JSONDecodeError as exc:
        return EndpointProbe(
            healthy=False,
            url=url,
            status_code=status_code,
            reason=f"json_error:{type(exc).__name__}:{exc}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            summary={},
        )
    except Exception as exc:  # noqa: BLE001 - watchdog must be fail-soft.
        return EndpointProbe(
            healthy=False,
            url=url,
            status_code=status_code,
            reason=f"probe_error:{type(exc).__name__}:{exc}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            summary={},
        )

    if status_code != 200 or not isinstance(payload, dict):
        return EndpointProbe(
            healthy=False,
            url=url,
            status_code=status_code,
            reason=f"unexpected_status:{status_code}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            summary={},
        )

    return EndpointProbe(
        healthy=True,
        url=url,
        status_code=status_code,
        reason="ok",
        elapsed_ms=int((time.monotonic() - started) * 1000),
        summary=_summary_view(payload),
        truth_summary_line=str(payload.get("truth_summary_line") or ""),
    )


def read_reverse_proxy_target(caddyfile_path: Path) -> tuple[str | None, str]:
    """Return the first Caddy reverse_proxy target and a short reason."""
    path = Path(caddyfile_path)
    if not path.exists():
        return None, "missing_caddyfile"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"read_failed:{type(exc).__name__}:{exc}"
    match = REVERSE_PROXY_RE.search(text)
    if not match:
        return None, "missing_reverse_proxy"
    return match.group(2).strip(), "ok"


def read_cloudflare_ingress_target(
    cloudflared_config_path: Path,
    public_hostname: str,
) -> tuple[str | None, str]:
    """Return the configured Cloudflare ingress target for one hostname."""
    path = Path(cloudflared_config_path)
    if not path.exists():
        return None, "missing_cloudflared_config"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return None, f"read_cloudflared_failed:{type(exc).__name__}:{exc}"

    current_hostname: str | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- hostname:") or line.startswith("hostname:"):
            current_hostname = line.split(":", 1)[1].strip().strip('"').strip("'")
            continue
        if line.startswith("service:") and current_hostname == public_hostname:
            target = _normalize_service_target(line.split(":", 1)[1].strip())
            if target is None:
                return None, "invalid_cloudflared_service"
            return target, "ok"
    return None, "missing_cloudflared_hostname"


def read_active_route_target(
    caddyfile_path: Path,
    *,
    cloudflared_config_path: Path = DEFAULT_CLOUDFLARED_CONFIG_PATH,
    public_hostname: str = DEFAULT_PUBLIC_HOSTNAME,
) -> tuple[str | None, str]:
    """Read the active route target from legacy Caddy or direct tunnel config."""
    direct_target, direct_reason = read_cloudflare_ingress_target(cloudflared_config_path, public_hostname)
    if direct_target is not None:
        return direct_target, "cloudflared_ingress_ok"
    target, reason = read_reverse_proxy_target(caddyfile_path)
    if target is not None:
        return target, reason
    if reason not in {"missing_caddyfile", "missing_reverse_proxy"}:
        return target, reason
    return None, direct_reason if direct_reason != "missing_cloudflared_config" else reason


def _default_public_edge_url() -> str:
    explicit = os.getenv("ETA_PUBLIC_EDGE_URL", "").strip()
    if explicit:
        return explicit
    direct_target, direct_reason = read_cloudflare_ingress_target(
        DEFAULT_CLOUDFLARED_CONFIG_PATH,
        DEFAULT_PUBLIC_HOSTNAME,
    )
    if direct_target is not None and direct_reason == "ok":
        return _service_url_from_target(direct_target, path="/api/dashboard/live-summary")
    return "http://127.0.0.1:8081/api/dashboard/live-summary"


def _default_canonical_edge_url() -> str:
    return os.getenv("ETA_PUBLIC_EDGE_CANONICAL_URL", "http://127.0.0.1:8421/api/dashboard/live-summary")


def _default_expected_target() -> str:
    explicit = os.getenv("ETA_PUBLIC_EDGE_EXPECTED_TARGET", "").strip()
    if explicit:
        return explicit
    if _cloudflare_mode_active():
        return DEFAULT_DIRECT_TARGET
    return "127.0.0.1:8421"


DEFAULT_PUBLIC_URL = _default_public_edge_url()
DEFAULT_CANONICAL_URL = _default_canonical_edge_url()
DEFAULT_EXPECTED_TARGET = _default_expected_target()


def evaluate_route(
    *,
    public_probe: EndpointProbe,
    canonical_probe: EndpointProbe,
    target: str | None,
    expected_target: str,
) -> tuple[bool, list[str]]:
    """Determine whether the public edge and canonical bridge are aligned."""
    reasons: list[str] = []
    if not public_probe.healthy:
        reasons.append(f"public_probe:{public_probe.reason}")
    if not canonical_probe.healthy:
        reasons.append(f"canonical_probe:{canonical_probe.reason}")
    if public_probe.healthy and canonical_probe.healthy:
        if public_probe.summary != canonical_probe.summary:
            reasons.append("summary_mismatch")
        if public_probe.truth_summary_line != canonical_probe.truth_summary_line:
            reasons.append("truth_line_mismatch")
    if target != expected_target:
        reasons.append(f"route_target:{target or 'missing'}")
    return not reasons, reasons


def validate_caddyfile(caddy_exe: Path, caddyfile_path: Path) -> tuple[bool, str]:
    """Validate one Caddyfile with the runtime Caddy binary."""
    exe = Path(caddy_exe)
    if not exe.exists():
        return False, f"missing_caddy_exe:{exe}"
    path = Path(caddyfile_path)
    if not path.exists():
        return False, f"missing_caddyfile:{path}"
    try:
        result = subprocess.run(  # noqa: S603
            [
                str(exe),
                "validate",
                "--config",
                str(path),
                "--adapter",
                "caddyfile",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"caddy_validate_failed:{type(exc).__name__}:{exc}"
    if result.returncode == 0:
        return True, "caddy_validate_ok"
    message = (result.stderr or result.stdout or "").strip().replace("\n", " ")
    return False, f"caddy_validate_rc={result.returncode}:{message[:240]}"


def restart_edge_service(service_name: str = DEFAULT_SERVICE_NAME) -> tuple[bool, str]:
    """Restart or start the public edge Windows service."""
    command = (
        f"$svc = Get-Service -Name '{service_name}' -ErrorAction Stop; "
        f"if ($svc.Status -eq 'Running') {{ Restart-Service -Name '{service_name}' -Force }} "
        f"else {{ Start-Service -Name '{service_name}' }}"
    )
    try:
        result = subprocess.run(  # noqa: S603, S607 - fixed Windows command.
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except FileNotFoundError:
        return False, "powershell_not_found"
    except Exception as exc:  # noqa: BLE001
        return False, f"service_restart_failed:{type(exc).__name__}:{exc}"

    if result.returncode == 0:
        return True, "service_restart_ok"
    message = (result.stderr or result.stdout or "").strip().replace("\n", " ")
    return False, f"service_restart_rc={result.returncode}:{message[:240]}"


def rewrite_cloudflare_ingress_target(
    cloudflared_config_path: Path,
    *,
    public_hostname: str,
    expected_target: str,
) -> tuple[bool, str | None, str | None, str | None, str]:
    """Rewrite one Cloudflare ingress service target in-place."""
    path = Path(cloudflared_config_path)
    if not path.exists():
        return False, None, None, None, "missing_cloudflared_config"
    try:
        original_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return False, None, None, None, f"read_cloudflared_failed:{type(exc).__name__}:{exc}"

    lines = list(original_lines)
    current_hostname: str | None = None
    previous_target: str | None = None
    line_index: int | None = None
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- hostname:") or line.startswith("hostname:"):
            current_hostname = line.split(":", 1)[1].strip().strip('"').strip("'")
            continue
        if line.startswith("service:") and current_hostname == public_hostname:
            previous_target = _normalize_service_target(line.split(":", 1)[1].strip())
            line_index = idx
            break

    if line_index is None or previous_target is None:
        return False, previous_target, previous_target, None, "missing_cloudflared_hostname"

    if previous_target == expected_target:
        return True, previous_target, previous_target, None, "cloudflared_unchanged"

    indent = lines[line_index][: len(lines[line_index]) - len(lines[line_index].lstrip())]
    updated_lines = list(lines)
    updated_lines[line_index] = f"{indent}service: http://{expected_target}"
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    backup = Path(f"{path}.route_watchdog_backup.{stamp}")
    try:
        backup.write_text("\n".join(original_lines) + "\n", encoding="utf-8")
        path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    except OSError as exc:
        return False, previous_target, previous_target, None, f"write_cloudflared_failed:{type(exc).__name__}:{exc}"
    return True, previous_target, expected_target, str(backup), "cloudflared_rewrite_ok"


def repair_public_edge_route(
    *,
    caddyfile_path: Path = DEFAULT_CADDYFILE_PATH,
    expected_target: str = DEFAULT_EXPECTED_TARGET,
    caddy_exe: Path = DEFAULT_CADDY_EXE,
    service_name: str = DEFAULT_SERVICE_NAME,
    cloudflared_config_path: Path = DEFAULT_CLOUDFLARED_CONFIG_PATH,
    public_hostname: str = DEFAULT_PUBLIC_HOSTNAME,
    cloudflared_service_name: str = DEFAULT_CLOUDFLARED_SERVICE_NAME,
    validate_fn: Callable[[Path, Path], tuple[bool, str]] = validate_caddyfile,
    restart_fn: Callable[[str], tuple[bool, str]] = restart_edge_service,
) -> RepairResult:
    """Normalize the Caddy reverse proxy target and restart the edge service."""
    direct_target, direct_reason = read_cloudflare_ingress_target(
        Path(cloudflared_config_path),
        public_hostname,
    )
    if direct_target is not None:
        changed, previous_target, current_target, backup_path, rewrite_reason = rewrite_cloudflare_ingress_target(
            cloudflared_config_path,
            public_hostname=public_hostname,
            expected_target=expected_target,
        )
        restart_ok, restart_reason = restart_fn(cloudflared_service_name)
        return RepairResult(
            ok=changed and restart_ok,
            changed_caddyfile=bool(previous_target != current_target),
            previous_target=previous_target,
            current_target=current_target,
            restart_ok=restart_ok,
            reason="ok" if changed and restart_ok else "restart_failed",
            backup_path=backup_path,
            validation_reason=rewrite_reason if direct_reason == "ok" else direct_reason,
            restart_reason=restart_reason,
        )

    path = Path(caddyfile_path)
    previous_target, target_reason = read_reverse_proxy_target(path)
    if previous_target is None:
        changed, previous_target, current_target, backup_path, rewrite_reason = rewrite_cloudflare_ingress_target(
            cloudflared_config_path,
            public_hostname=public_hostname,
            expected_target=expected_target,
        )
        if rewrite_reason in {"missing_cloudflared_config", "missing_cloudflared_hostname"}:
            return RepairResult(
                ok=False,
                changed_caddyfile=False,
                previous_target=None,
                current_target=None,
                restart_ok=False,
                reason=target_reason if target_reason != "missing_caddyfile" else rewrite_reason,
            )
        restart_ok, restart_reason = restart_fn(cloudflared_service_name)
        return RepairResult(
            ok=changed and restart_ok,
            changed_caddyfile=bool(previous_target != current_target),
            previous_target=previous_target,
            current_target=current_target,
            restart_ok=restart_ok,
            reason="ok" if changed and restart_ok else "restart_failed",
            backup_path=backup_path,
            validation_reason=rewrite_reason,
            restart_reason=restart_reason,
        )

    try:
        original_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return RepairResult(
            ok=False,
            changed_caddyfile=False,
            previous_target=previous_target,
            current_target=previous_target,
            restart_ok=False,
            reason=f"read_failed:{type(exc).__name__}:{exc}",
        )

    changed_caddyfile = previous_target != expected_target
    backup_path: str | None = None
    validation_reason: str | None = None
    current_target = previous_target

    if changed_caddyfile:
        updated_text = REVERSE_PROXY_RE.sub(
            lambda match: f"{match.group(1)}{expected_target}{match.group(3)}",
            original_text,
            count=1,
        )
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        backup = Path(f"{path}.route_watchdog_backup.{stamp}")
        try:
            backup.write_text(original_text, encoding="utf-8")
            path.write_text(updated_text, encoding="utf-8")
        except OSError as exc:
            return RepairResult(
                ok=False,
                changed_caddyfile=False,
                previous_target=previous_target,
                current_target=previous_target,
                restart_ok=False,
                reason=f"write_failed:{type(exc).__name__}:{exc}",
            )
        backup_path = str(backup)
        valid, validation_reason = validate_fn(caddy_exe, path)
        if not valid:
            with contextlib.suppress(OSError):
                path.write_text(original_text, encoding="utf-8")
            return RepairResult(
                ok=False,
                changed_caddyfile=True,
                previous_target=previous_target,
                current_target=previous_target,
                restart_ok=False,
                reason="validation_failed",
                backup_path=backup_path,
                validation_reason=validation_reason,
            )
        current_target = expected_target

    restart_ok, restart_reason = restart_fn(service_name)
    return RepairResult(
        ok=restart_ok,
        changed_caddyfile=changed_caddyfile,
        previous_target=previous_target,
        current_target=current_target,
        restart_ok=restart_ok,
        reason="ok" if restart_ok else "restart_failed",
        backup_path=backup_path,
        validation_reason=validation_reason,
        restart_reason=restart_reason,
    )


def _record(decision: RouteWatchdogDecision) -> None:
    with contextlib.suppress(Exception):
        record_uptime_event(
            component="public_edge_route_watchdog",
            event=decision.action,
            reason=";".join(decision.mismatch_reasons) or "ok",
            extra=decision.to_dict(),
        )


def _write_heartbeat(path: Path, decision: RouteWatchdogDecision) -> None:
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "component": "public_edge_route_watchdog",
        "decision": decision.to_dict(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("public edge route watchdog heartbeat write failed: %s", exc)


def run_once(
    *,
    public_url: str = DEFAULT_PUBLIC_URL,
    canonical_url: str = DEFAULT_CANONICAL_URL,
    expected_target: str = DEFAULT_EXPECTED_TARGET,
    caddyfile_path: Path = DEFAULT_CADDYFILE_PATH,
    caddy_exe: Path = DEFAULT_CADDY_EXE,
    service_name: str = DEFAULT_SERVICE_NAME,
    cloudflared_config_path: Path = DEFAULT_CLOUDFLARED_CONFIG_PATH,
    public_hostname: str = DEFAULT_PUBLIC_HOSTNAME,
    cloudflared_service_name: str = DEFAULT_CLOUDFLARED_SERVICE_NAME,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    restart_delay_s: float = DEFAULT_RESTART_DELAY_S,
    heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH,
    probe_fn: Callable[[str], EndpointProbe] | None = None,
    inspect_target_fn: Callable[[Path], tuple[str | None, str]] | None = None,
    repair_fn: Callable[..., RepairResult] = repair_public_edge_route,
) -> RouteWatchdogDecision:
    """Run one public-edge route watchdog tick and write the canonical heartbeat."""
    if probe_fn is None:
        def probe_fn(url: str) -> EndpointProbe:
            return probe_endpoint(url, timeout_s=timeout_s)

    if inspect_target_fn is None:
        def inspect_target_fn(_path: Path) -> tuple[str | None, str]:
            return read_active_route_target(
                Path(caddyfile_path),
                cloudflared_config_path=Path(cloudflared_config_path),
                public_hostname=public_hostname,
            )

    target_before, _ = inspect_target_fn(Path(caddyfile_path))
    public_probe = probe_fn(public_url)
    canonical_probe = probe_fn(canonical_url)
    route_ok_before, mismatch_reasons = evaluate_route(
        public_probe=public_probe,
        canonical_probe=canonical_probe,
        target=target_before,
        expected_target=expected_target,
    )
    decision = RouteWatchdogDecision(
        checked_at=datetime.now(UTC).isoformat(),
        action="noop" if route_ok_before else "repair_requested",
        route_ok_before=route_ok_before,
        route_ok_after=None,
        expected_target=expected_target,
        target_before=target_before,
        target_after=target_before,
        public_probe=public_probe,
        canonical_probe=canonical_probe,
        mismatch_reasons=mismatch_reasons,
    )

    if not route_ok_before:
        repair = repair_fn(
            caddyfile_path=Path(caddyfile_path),
            expected_target=expected_target,
            caddy_exe=Path(caddy_exe),
            service_name=service_name,
            cloudflared_config_path=Path(cloudflared_config_path),
            public_hostname=public_hostname,
            cloudflared_service_name=cloudflared_service_name,
        )
        decision.repair = repair
        if repair.ok and restart_delay_s > 0:
            time.sleep(restart_delay_s)
        decision.post_public_probe = probe_fn(public_url)
        decision.post_canonical_probe = probe_fn(canonical_url)
        target_after, _ = inspect_target_fn(Path(caddyfile_path))
        decision.target_after = target_after
        route_ok_after, post_reasons = evaluate_route(
            public_probe=decision.post_public_probe,
            canonical_probe=decision.post_canonical_probe,
            target=target_after,
            expected_target=expected_target,
        )
        decision.route_ok_after = route_ok_after
        if route_ok_after:
            decision.action = "repaired" if repair.changed_caddyfile else "restarted"
            decision.mismatch_reasons = post_reasons
        else:
            decision.action = "repair_failed"
            decision.mismatch_reasons = post_reasons

    _record(decision)
    _write_heartbeat(heartbeat_path, decision)
    return decision


def _exit_code(decision: RouteWatchdogDecision) -> int:
    if decision.route_ok_before:
        return 0
    if decision.route_ok_after:
        return 0
    if decision.repair is not None and not decision.repair.ok:
        return 2
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-url", default=DEFAULT_PUBLIC_URL)
    parser.add_argument("--canonical-url", default=DEFAULT_CANONICAL_URL)
    parser.add_argument("--expected-target", default=DEFAULT_EXPECTED_TARGET)
    parser.add_argument("--caddyfile", type=Path, default=DEFAULT_CADDYFILE_PATH)
    parser.add_argument("--caddy-exe", type=Path, default=DEFAULT_CADDY_EXE)
    parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME)
    parser.add_argument("--cloudflared-config", type=Path, default=DEFAULT_CLOUDFLARED_CONFIG_PATH)
    parser.add_argument("--public-hostname", default=DEFAULT_PUBLIC_HOSTNAME)
    parser.add_argument("--cloudflared-service-name", default=DEFAULT_CLOUDFLARED_SERVICE_NAME)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--restart-delay-s", type=float, default=DEFAULT_RESTART_DELAY_S)
    parser.add_argument("--heartbeat-path", type=Path, default=DEFAULT_HEARTBEAT_PATH)
    parser.add_argument("--interval-s", type=float, default=300.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    def tick() -> RouteWatchdogDecision:
        return run_once(
            public_url=args.public_url,
            canonical_url=args.canonical_url,
            expected_target=args.expected_target,
            caddyfile_path=args.caddyfile,
            caddy_exe=args.caddy_exe,
            service_name=args.service_name,
            cloudflared_config_path=args.cloudflared_config,
            public_hostname=args.public_hostname,
            cloudflared_service_name=args.cloudflared_service_name,
            timeout_s=args.timeout_s,
            restart_delay_s=args.restart_delay_s,
            heartbeat_path=args.heartbeat_path,
        )

    if args.once:
        decision = tick()
        if args.json:
            print(json.dumps(decision.to_dict(), indent=2))
        else:
            logger.info(
                "public edge route watchdog: action=%s route_ok_before=%s route_ok_after=%s",
                decision.action,
                decision.route_ok_before,
                decision.route_ok_after,
            )
        return _exit_code(decision)

    while True:
        try:
            decision = tick()
            logger.info(
                "public edge route watchdog: action=%s route_ok_before=%s route_ok_after=%s",
                decision.action,
                decision.route_ok_before,
                decision.route_ok_after,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("public edge route watchdog tick failed: %s", exc)
        time.sleep(max(30.0, float(args.interval_s)))


if __name__ == "__main__":
    raise SystemExit(main())
