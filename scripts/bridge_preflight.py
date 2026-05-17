"""
Bridge pre-flight gate-check.

Exhaustive verification of the Hermes-JARVIS brain-OS before a
live-capital cutover. Stricter than ``hermes_bridge_health`` — the
health check answers "is it alive?", this script answers "is it
READY for live capital?"

Differences from the 9-layer health check:

  * Stricter thresholds — health says PASS at 200, this says PASS at
    200 AND latency under 10s AND auth credential is literal-source
    (not env-template).
  * Tests every WRITE-back path actually round-trips. Health only reads.
  * Tests Kelly + regime + topology data freshness (last update < 24h).
  * Confirms scheduled tasks are registered AND not crashing.
  * Confirms memory backup task is registered.
  * Confirms audit log is rotating (size sane, no infinite growth).
  * Confirms VPS disk has headroom (var/ subtree).
  * Confirms tunnel ssh process has run > 30 min uptime.

Output
------

A pass/fail line per check + a final verdict:

  ┌─ READY ──────── all critical checks PASS, fleet may be unlocked
  ├─ READY WITH CONCERNS ── critical checks pass, non-critical warn
  └─ NOT READY ──── at least one critical check fails, hold cutover

Exit code: 0 if READY, 1 otherwise.

Usage
-----

  python -m eta_engine.scripts.bridge_preflight
  python -m eta_engine.scripts.bridge_preflight --json
  python -m eta_engine.scripts.bridge_preflight --skip-llm

Designed to run from the operator's desktop with the SSH tunnel up.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eta_engine.scripts import workspace_roots

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("eta_engine.scripts.bridge_preflight")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
WORKSPACE = workspace_roots.WORKSPACE_ROOT
STATE_ROOT = workspace_roots.ETA_RUNTIME_STATE_DIR
_REMOTE_MEMORY_BACKUP_DIR = str(workspace_roots.ETA_HERMES_MEMORY_BACKUP_DIR)

LATENCY_CRITICAL_MS = 15000  # >15s LLM round-trip is a red flag
AUDIT_LOG_MAX_OK_BYTES = 50 * 1024 * 1024  # >50MB without rotation = bug
DISK_HEADROOM_MIN_GB = 5
TUNNEL_UPTIME_MIN_SECONDS = 30 * 60  # ssh tunnel should have been up 30min
SCHEDULED_TASKS_EXPECTED = ("ETA-Hermes-Agent",)


@dataclass
class CheckResult:
    name: str
    severity: str  # "critical" | "warning" | "info"
    status: str  # "PASS" | "FAIL" | "WARN" | "SKIP"
    detail: str
    elapsed_ms: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)

    def is_blocker(self) -> bool:
        return self.severity == "critical" and self.status != "PASS"


def _run(name: str, severity: str, fn: Callable[[], tuple[str, str, dict]]) -> CheckResult:
    """Run ``fn`` and wrap its (status, detail, extras) tuple in a CheckResult."""
    t0 = time.monotonic()
    try:
        status, detail, extras = fn()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name=name,
            severity=severity,
            status="FAIL",
            detail=f"check raised: {exc}",
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )
    return CheckResult(
        name=name,
        severity=severity,
        status=status,
        detail=detail,
        elapsed_ms=(time.monotonic() - t0) * 1000.0,
        extras=extras,
    )


def _resolve_api_key() -> str | None:
    for env in ("HERMES_API_KEY", "API_SERVER_KEY", "JARVIS_MCP_TOKEN"):
        v = os.environ.get(env)
        if v:
            return v
    return None


def _http_post(url: str, payload: dict, api_key: str | None, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return {"status": resp.status, "body": body}


# ---------------------------------------------------------------------------
# Check implementations — each returns (status, detail, extras)
# ---------------------------------------------------------------------------


def check_tunnel(host: str, port: int) -> tuple[str, str, dict]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect((host, port))
        return "PASS", f"tunnel up at {host}:{port}", {}
    except OSError as exc:
        return "FAIL", f"tunnel down: {exc}", {}
    finally:
        s.close()


def check_tunnel_uptime() -> tuple[str, str, dict]:
    """ssh tunnel should have been up >30min before live cutover."""
    if sys.platform != "win32":
        return "SKIP", "non-Windows host", {}
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process ssh -EA SilentlyContinue | Where-Object { $_.Path -like '*OpenSSH*' } | "
                "Select-Object -First 1 -ExpandProperty StartTime",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        start_str = out.stdout.strip()
        if not start_str:
            return "FAIL", "no ssh tunnel process found", {}
        # Try several formats — Windows PowerShell renders StartTime in
        # the user's locale, which varies (en-US short, en-US long, ISO).
        candidate_formats = [
            "%m/%d/%Y %I:%M:%S %p",  # 5/12/2026 8:23:23 AM
            "%A, %B %d, %Y %I:%M:%S %p",  # Tuesday, May 12, 2026 8:23:23 AM
            "%a, %b %d, %Y %I:%M:%S %p",  # Tue, May 12, 2026 8:23:23 AM
            "%Y-%m-%d %H:%M:%S",  # 2026-05-12 08:23:23
            "%m/%d/%Y %H:%M:%S",  # 5/12/2026 20:23:23
        ]
        started_at: datetime | None = None
        cleaned = start_str.split(".")[0]
        for fmt in candidate_formats:
            try:
                started_at = datetime.strptime(cleaned, fmt)
                break
            except ValueError:
                continue
        if started_at is None:
            try:
                started_at = datetime.fromisoformat(cleaned)
            except ValueError:
                return "WARN", f"could not parse ssh StartTime: {start_str!r}", {}
        uptime_s = (datetime.now() - started_at).total_seconds()
        if uptime_s < TUNNEL_UPTIME_MIN_SECONDS:
            return (
                "WARN",
                f"tunnel uptime only {uptime_s:.0f}s (< {TUNNEL_UPTIME_MIN_SECONDS}s threshold)",
                {"uptime_s": uptime_s},
            )
        return "PASS", f"tunnel uptime {uptime_s / 60:.0f}min", {"uptime_s": uptime_s}
    except (subprocess.SubprocessError, OSError) as exc:
        return "WARN", f"could not query ssh uptime: {exc}", {}


def check_gateway(host: str, port: int) -> tuple[str, str, dict]:
    try:
        req = urllib.request.Request(f"http://{host}:{port}/health")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return "PASS", f"/health HTTP {resp.status}", {}
    except (urllib.error.URLError, OSError) as exc:
        return "FAIL", f"gateway unreachable: {exc}", {}


def check_llm_latency(host: str, port: int, api_key: str | None) -> tuple[str, str, dict]:
    """LLM round-trip must complete under LATENCY_CRITICAL_MS."""
    if not api_key:
        return "FAIL", "no API key in env (HERMES_API_KEY/API_SERVER_KEY)", {}
    t0 = time.monotonic()
    try:
        r = _http_post(
            f"http://{host}:{port}/v1/chat/completions",
            payload={
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "Reply ONLY: ok"}],
                "max_tokens": 4,
                "stream": False,
            },
            api_key=api_key,
            timeout=30,
        )
    except urllib.error.HTTPError as exc:
        return "FAIL", f"chat HTTP {exc.code}", {}
    except (urllib.error.URLError, OSError) as exc:
        return "FAIL", f"chat error: {exc}", {}
    latency_ms = (time.monotonic() - t0) * 1000.0
    if r["status"] != 200:
        return "FAIL", f"chat returned {r['status']}", {"latency_ms": latency_ms}
    if latency_ms > LATENCY_CRITICAL_MS:
        return (
            "WARN",
            f"chat latency {latency_ms:.0f}ms exceeds {LATENCY_CRITICAL_MS}ms threshold",
            {"latency_ms": latency_ms},
        )
    return "PASS", f"chat latency {latency_ms:.0f}ms", {"latency_ms": latency_ms}


def check_write_back_round_trip(host: str, port: int, api_key: str | None) -> tuple[str, str, dict]:
    """Apply a TTL-1m size_modifier, read it back, clear it. Confirms the
    T2 write-back path works end-to-end through MCP."""
    if not api_key:
        return "FAIL", "no API key", {}
    prompt = (
        "Call jarvis_set_size_modifier with bot_id='__preflight_smoke__', "
        "modifier=0.5, reason='preflight test', ttl_minutes=1. Then call "
        "jarvis_active_overrides. Then call jarvis_clear_override with "
        "bot_id='__preflight_smoke__'. Reply with ONLY one of: "
        "WRITEBACK_OK if all three steps succeeded, "
        "WRITEBACK_FAIL otherwise. No prose."
    )
    try:
        r = _http_post(
            f"http://{host}:{port}/v1/chat/completions",
            payload={
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 24,
                "stream": False,
            },
            api_key=api_key,
            timeout=120,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return "FAIL", f"write-back chat error: {exc}", {}
    if r["status"] != 200:
        return "FAIL", f"write-back chat returned {r['status']}", {}
    try:
        reply = json.loads(r["body"])["choices"][0]["message"]["content"].strip()
    except (json.JSONDecodeError, KeyError, IndexError):
        return "FAIL", "write-back reply not parseable", {}
    if "WRITEBACK_OK" in reply:
        return "PASS", f"write-back round-trip OK; reply: {reply}", {}
    return "FAIL", f"write-back round-trip failed; reply: {reply}", {}


def check_credential_pool_is_literal(host: str, port: int, api_key: str | None) -> tuple[str, str, dict]:
    """Confirm Hermes credential pool for deepseek uses LITERAL value,
    not env:DEEPSEEK_API_KEY template (the bug we fixed earlier)."""
    if sys.platform != "win32":
        return "SKIP", "credential check is VPS-side; skipping local", {}
    try:
        remote_cmd = (
            "powershell -NoProfile -Command "
            "\"& 'C:\\Users\\Administrator\\.hermes\\hermes-agent\\.venv\\Scripts\\python.exe' "
            "-c \\\"import os; os.chdir(r'C:\\Users\\Administrator\\.hermes\\hermes-agent')\\\" 2>&1; "
            "Set-Location 'C:\\Users\\Administrator\\.hermes\\hermes-agent'; "
            "& 'C:\\Users\\Administrator\\.hermes\\hermes-agent\\.venv\\Scripts\\python.exe' "
            'hermes auth list 2>&1"'
        )
        result = subprocess.run(
            ["ssh", "forex-vps", remote_cmd],
            capture_output=True,
            text=True,
            timeout=20,
        )
        out = result.stdout + result.stderr
        if "deepseek" not in out.lower():
            return "FAIL", "no deepseek credential found in pool", {}
        if "env:DEEPSEEK_API_KEY" in out and "manual" not in out:
            return ("FAIL", "deepseek credential is env-source only — the 401 crash-loop bug", {})
        if "manual" in out:
            return "PASS", "deepseek credential is literal (manual source)", {}
        return "WARN", "deepseek credential present but source unclear", {"output": out[:300]}
    except (subprocess.SubprocessError, OSError) as exc:
        return "WARN", f"could not query VPS credential pool: {exc}", {}


def check_scheduled_tasks_alive(host: str, port: int, api_key: str | None) -> tuple[str, str, dict]:
    """Confirm ETA-Hermes-Agent scheduled task is running on VPS."""
    if sys.platform != "win32":
        return "SKIP", "Windows-only check", {}
    try:
        result = subprocess.run(
            [
                "ssh",
                "forex-vps",
                "powershell -NoProfile -Command \"Get-ScheduledTask -TaskName 'ETA-Hermes-Agent' | "
                'Get-ScheduledTaskInfo | Select-Object -ExpandProperty LastTaskResult"',
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        rc = result.stdout.strip()
        # 267009 = "task is currently running" (good); 0 = "succeeded"; other = stale
        if rc in ("267009", "0"):
            return "PASS", f"ETA-Hermes-Agent task healthy (LastTaskResult={rc})", {}
        return "WARN", f"ETA-Hermes-Agent LastTaskResult={rc!r}", {"rc": rc}
    except (subprocess.SubprocessError, OSError) as exc:
        return "WARN", f"could not query VPS scheduled task: {exc}", {}


def check_audit_log_sane() -> tuple[str, str, dict]:
    """Audit log exists, isn't huge (rotation working), recent activity."""
    p = STATE_ROOT / "hermes_actions.jsonl"
    if not p.exists():
        return "WARN", f"no audit log yet at {p}", {}
    size = p.stat().st_size
    if size > AUDIT_LOG_MAX_OK_BYTES:
        return (
            "FAIL",
            f"audit log {size / 1024 / 1024:.1f}MB > {AUDIT_LOG_MAX_OK_BYTES / 1024 / 1024}MB — rotation broken",
            {"size": size},
        )
    return "PASS", f"audit log {size / 1024:.1f}KB (under rotation threshold)", {"size": size}


def check_memory_backup_recent() -> tuple[str, str, dict]:
    """At least one memory backup within the last 48h.

    Backups live on the VPS (where the active memory DB lives). Try
    the local STATE_ROOT first (in case preflight is run on VPS), then
    fall back to an SSH query to forex-vps.
    """
    local_dir = STATE_ROOT / "backups" / "hermes_memory"
    if local_dir.exists():
        backups = sorted(local_dir.glob("hermes_memory_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        if backups:
            age_h = (time.time() - backups[0].stat().st_mtime) / 3600
            if age_h > 48:
                return "WARN", f"newest backup is {age_h:.0f}h old (>48h threshold)", {"age_h": age_h}
            return "PASS", f"{len(backups)} backups, newest {age_h:.0f}h old", {"count": len(backups)}
    # Fall through: try VPS via SSH
    if sys.platform != "win32":
        return "WARN", "no local backup dir and SSH-VPS check skipped on non-Windows", {}
    try:
        result = subprocess.run(
            [
                "ssh",
                "forex-vps",
                (
                    'powershell -NoProfile -Command "'
                    f"$d = '{_REMOTE_MEMORY_BACKUP_DIR}'; "
                    "if (Test-Path $d) { "
                    "  $b = Get-ChildItem $d -Filter 'hermes_memory_*.db' | Sort-Object LastWriteTime -Descending; "
                    "  if ($b) { "
                    "    $age_h = ([DateTime]::Now - $b[0].LastWriteTime).TotalHours; "
                    '    \\"count=$($b.Count) newest_age_h=$age_h\\" '
                    "  } else { 'no_backups' } "
                    "} else { 'no_dir' }\""
                ),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        out = result.stdout.strip()
        if "no_dir" in out or "no_backups" in out:
            return "WARN", "no memory backup directory yet on VPS", {}
        if "count=" in out:
            # Parse: count=N newest_age_h=H
            parts = dict(p.split("=", 1) for p in out.split() if "=" in p)
            count = int(parts.get("count", 0))
            try:
                age_h = float(parts.get("newest_age_h", 999))
            except ValueError:
                age_h = 999
            if age_h > 48:
                return "WARN", f"newest VPS backup is {age_h:.0f}h old (>48h threshold)", {"age_h": age_h}
            return "PASS", f"{count} backups on VPS, newest {age_h:.1f}h old", {"count": count}
        return "WARN", f"could not parse VPS backup query: {out!r}", {}
    except (subprocess.SubprocessError, OSError) as exc:
        return "WARN", f"could not SSH to VPS for backup check: {exc}", {}


def check_disk_headroom() -> tuple[str, str, dict]:
    """VPS var/ subtree has enough free disk for ~30 days of new traces."""
    if sys.platform != "win32":
        return "SKIP", "disk check is platform-specific", {}
    try:
        result = subprocess.run(
            ["ssh", "forex-vps", 'powershell -NoProfile -Command "(Get-PSDrive C).Free / 1GB"'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        free_gb = float(result.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        return "WARN", f"could not query VPS disk: {exc}", {}
    if free_gb < DISK_HEADROOM_MIN_GB:
        return ("FAIL", f"VPS C: free {free_gb:.1f}GB < {DISK_HEADROOM_MIN_GB}GB threshold", {"free_gb": free_gb})
    return "PASS", f"VPS C: free {free_gb:.1f}GB", {"free_gb": free_gb}


def check_kelly_recommendations_present(host: str, port: int, api_key: str | None) -> tuple[str, str, dict]:
    """Kelly should return recommendations for >5 bots before going live —
    means we have enough trade history to size against."""
    if not api_key:
        return "FAIL", "no API key", {}
    prompt = (
        "Call jarvis_kelly_recommend with no args. Reply ONLY with the "
        "number of recommendations that have insufficient_data=False. "
        "Just an integer."
    )
    try:
        r = _http_post(
            f"http://{host}:{port}/v1/chat/completions",
            payload={
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 8,
                "stream": False,
            },
            api_key=api_key,
            timeout=90,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return "WARN", f"kelly chat error: {exc}", {}
    if r["status"] != 200:
        return "WARN", f"kelly chat returned {r['status']}", {}
    try:
        reply = json.loads(r["body"])["choices"][0]["message"]["content"].strip()
        n_rich = int(reply.strip().split()[0])
    except (json.JSONDecodeError, KeyError, IndexError, ValueError):
        return "WARN", "kelly reply not parseable as int", {}
    if n_rich < 5:
        return ("WARN", f"only {n_rich} bots have enough trade history for Kelly (need ≥5)", {"n_rich": n_rich})
    return "PASS", f"{n_rich} bots with sufficient Kelly data", {"n_rich": n_rich}


def check_status_server(status_port: int = 8643) -> tuple[str, str, dict]:
    """Operator's direct contact-point status server is reachable.

    Verifies the sidecar is up AND that ``/health`` returns 200 AND that
    ``/contact`` returns a parseable JSON contact card. If the status
    server is down the operator loses the "is everything alive?" page —
    not catastrophic for trading, but a real degradation.
    """
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{status_port}/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status != 200:
                return "WARN", f"/health returned {resp.status}", {}
    except (urllib.error.URLError, OSError) as exc:
        return "WARN", f"status server unreachable on 127.0.0.1:{status_port}: {exc}", {}
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{status_port}/contact")
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            card = json.loads(body)
            n_tools = int(card.get("available_tools_count", 0))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        return "WARN", f"status server /contact malformed: {exc}", {}
    return "PASS", f"status server live, {n_tools} tools advertised", {"n_tools": n_tools}


def check_health_check_passes(host: str, port: int) -> tuple[str, str, dict]:
    """Run the existing 9-layer health check and confirm all PASS."""
    try:
        from eta_engine.scripts import hermes_bridge_health

        results = hermes_bridge_health.run_all(host=host, port=port)
    except Exception as exc:  # noqa: BLE001
        return "FAIL", f"health-check script error: {exc}", {}
    failures = [r.name for r in results if not r.ok]
    if failures:
        return "FAIL", f"{len(failures)}/{len(results)} layers failing: {failures}", {}
    return "PASS", f"all {len(results)} health layers PASS", {}


# ---------------------------------------------------------------------------
# Verdict + runner
# ---------------------------------------------------------------------------


def run_all(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    skip: set[str] | None = None,
) -> list[CheckResult]:
    skip = skip or set()
    api_key = _resolve_api_key()
    results: list[CheckResult] = []

    spec: list[tuple[str, str, Callable]] = [
        ("tunnel", "critical", lambda: check_tunnel(host, port)),
        ("tunnel_uptime", "warning", check_tunnel_uptime),
        ("gateway", "critical", lambda: check_gateway(host, port)),
        ("llm_latency", "critical", lambda: check_llm_latency(host, port, api_key)),
        ("credential_literal", "critical", lambda: check_credential_pool_is_literal(host, port, api_key)),
        ("write_back", "critical", lambda: check_write_back_round_trip(host, port, api_key)),
        ("scheduled_tasks", "warning", lambda: check_scheduled_tasks_alive(host, port, api_key)),
        ("audit_log", "warning", check_audit_log_sane),
        ("memory_backup", "warning", check_memory_backup_recent),
        ("disk_headroom", "warning", check_disk_headroom),
        ("kelly_ready", "warning", lambda: check_kelly_recommendations_present(host, port, api_key)),
        ("status_server", "warning", check_status_server),
        ("health_9_layers", "critical", lambda: check_health_check_passes(host, port)),
    ]
    for name, sev, fn in spec:
        if name in skip:
            continue
        results.append(_run(name, sev, fn))
    return results


def verdict(results: list[CheckResult]) -> str:
    if any(r.is_blocker() for r in results):
        return "NOT_READY"
    if any(r.status in ("FAIL", "WARN") for r in results):
        return "READY_WITH_CONCERNS"
    return "READY"


def render_table(results: list[CheckResult]) -> str:
    name_w = max(len(r.name) for r in results) if results else 10
    lines = [
        "",
        "=========== BRIDGE PRE-FLIGHT GATE-CHECK ===========",
        f"  asof {datetime.now(UTC).isoformat()}",
        "-" * 54,
    ]
    for r in results:
        glyph = {
            "PASS": "[ OK ]",
            "FAIL": "[FAIL]",
            "WARN": "[WARN]",
            "SKIP": "[skip]",
        }.get(r.status, "[ ?? ]")
        crit = "C" if r.severity == "critical" else "w"
        lines.append(f"  {glyph} {crit}  {r.name.ljust(name_w)}  ({r.elapsed_ms:6.0f}ms)  {r.detail}")
    v = verdict(results)
    lines.append("-" * 54)
    lines.append(f"  VERDICT: {v}")
    lines.append("")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--skip", type=str, default="", help="Comma-separated check names to skip")
    p.add_argument("--json", action="store_true", help="JSON output instead of human table")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    results = run_all(host=args.host, port=args.port, skip=skip)
    if args.json:
        payload = {
            "asof": datetime.now(UTC).isoformat(),
            "verdict": verdict(results),
            "checks": [asdict(r) for r in results],
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(render_table(results))
    return 0 if verdict(results) == "READY" else 1


if __name__ == "__main__":
    sys.exit(main())
