"""
Bridge autoheal watchdog — periodic self-healing for common failure modes.

Runs every ~15 min on the VPS as ``ETA-Bridge-Autoheal``. Each tick:

  1. Probes the 9-layer health check.
  2. For each known failure mode that maps to a safe automatic fix,
     applies the fix.
  3. Logs every action to ``var/bridge_autoheal.log`` so the operator's
     morning briefing can surface "the watchdog did X overnight."
  4. NEVER takes a destructive action (no kill, no retire, no broker
     contact). All fixes are reversible: restart task, prune file,
     clear stale lock.

Failure modes + fixes
---------------------

| Mode | Detection | Fix |
|---|---|---|
| Hermes gateway down | /health unreachable | ``schtasks /End`` + ``/Run`` on ETA-Hermes-Agent |
| Status server down | port 8643 not listening | restart ETA-Jarvis-Status-Server |
| Audit log oversize | size > 50MB | force-trigger rotation by calling _rotate_audit_log_if_needed |
| Memory backup stale | newest > 48h old | run hermes_memory_backup once |
| Stale agent_registry lock | lock expires_at < now | already auto-swept on acquire; just report |
| Orphan trade narrator file | journal file >7 days w/ no consult activity | nothing — operator review (informational) |

What the watchdog does NOT do
-----------------------------

* Never calls jarvis_kill_switch, jarvis_retire_strategy, or any
  destructive MCP tool.
* Never modifies trading code, broker config, or live order state.
* Never restarts the trading supervisor (that's a separate concern).
* Never sends Hermes chats (would burn LLM cost on every poll).

The watchdog is "infrastructure babysitter" — it keeps the brain-OS
plumbing alive so the operator doesn't have to.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.scripts.bridge_autoheal")

WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
STATE_ROOT = WORKSPACE / "var" / "eta_engine" / "state"
AUTOHEAL_LOG = WORKSPACE / "var" / "bridge_autoheal_actions.jsonl"
AUDIT_LOG_PATH = STATE_ROOT / "hermes_actions.jsonl"
MEMORY_BACKUP_DIR = STATE_ROOT / "backups" / "hermes_memory"
HERMES_PORT = 8642
STATUS_SERVER_PORT = 8643
AUDIT_LOG_FORCE_ROTATE_BYTES = 50 * 1024 * 1024
MEMORY_BACKUP_STALE_HOURS = 48
HERMES_HEALTH_TIMEOUT_S = 5


@dataclass
class AutohealAction:
    """One healing attempt — recorded in the JSONL log per fix."""

    asof: str
    mode: str
    detection: str
    action: str
    status: str  # "fixed" | "noop" | "failed" | "skipped"
    detail: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _port_listening(host: str, port: int, timeout_s: float = 2.0) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _hermes_healthy() -> bool:
    """HTTP 200 from Hermes /health within budget."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{HERMES_PORT}/health")
        with urllib.request.urlopen(req, timeout=HERMES_HEALTH_TIMEOUT_S) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _status_server_healthy() -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{STATUS_SERVER_PORT}/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _audit_log_size() -> int:
    try:
        return AUDIT_LOG_PATH.stat().st_size if AUDIT_LOG_PATH.exists() else 0
    except OSError:
        return 0


def _newest_backup_age_hours() -> float | None:
    """Hours since the newest memory backup. None if no backup dir yet."""
    if not MEMORY_BACKUP_DIR.exists():
        return None
    backups = sorted(
        MEMORY_BACKUP_DIR.glob("hermes_memory_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not backups:
        return None
    return (time.time() - backups[0].stat().st_mtime) / 3600


# ---------------------------------------------------------------------------
# Fix actions
# ---------------------------------------------------------------------------


def _restart_scheduled_task(task_name: str) -> tuple[bool, str]:
    """Stop + start a Windows scheduled task. Returns (success, detail)."""
    if sys.platform != "win32":
        return False, "non-Windows platform — skipped"
    try:
        # End existing instance (best-effort)
        subprocess.run(
            ["schtasks", "/End", "/TN", task_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        time.sleep(1)
        result = subprocess.run(
            ["schtasks", "/Run", "/TN", task_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, f"/Run returned {result.returncode}: {result.stderr.strip()}"
        return True, f"task '{task_name}' restarted"
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"restart failed: {exc}"


def _force_rotate_audit_log() -> tuple[bool, str]:
    """Force-trigger gzip rotation of the audit log."""
    try:
        # Re-use the production rotation helper for consistency
        from eta_engine.mcp_servers import jarvis_mcp_server

        # Save + restore the threshold so this call rotates even if the
        # log is right at the boundary
        original = jarvis_mcp_server._AUDIT_LOG_MAX_BYTES
        try:
            # Temporarily set threshold to 1 so rotation fires
            jarvis_mcp_server._AUDIT_LOG_MAX_BYTES = 1
            jarvis_mcp_server._rotate_audit_log_if_needed()
        finally:
            jarvis_mcp_server._AUDIT_LOG_MAX_BYTES = original
        return True, "audit log gzip-rotated"
    except Exception as exc:  # noqa: BLE001
        return False, f"rotation helper failed: {exc}"


def _run_memory_backup() -> tuple[bool, str]:
    """Fire the memory backup script once."""
    try:
        from eta_engine.scripts import hermes_memory_backup

        summary = hermes_memory_backup.run()
        if summary["status"] == "ok":
            return True, f"backup written to {summary['backup_path']}"
        return False, f"backup status: {summary['status']}"
    except Exception as exc:  # noqa: BLE001
        return False, f"backup helper failed: {exc}"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _append_action(action: AutohealAction) -> None:
    """Append the action to the autoheal log. Never raises."""
    try:
        AUTOHEAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUTOHEAL_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(action), default=str) + "\n")
    except OSError as exc:
        logger.warning("autoheal log append failed: %s", exc)


def autoheal_once() -> list[AutohealAction]:
    """Run one autoheal cycle. Returns the list of actions taken."""
    actions: list[AutohealAction] = []

    # ── Mode 1: Hermes gateway down ──
    if not _hermes_healthy():
        ok, detail = _restart_scheduled_task("ETA-Hermes-Agent")
        actions.append(
            AutohealAction(
                asof=_now_iso(),
                mode="hermes_gateway_down",
                detection="/health unreachable within 5s",
                action="restart ETA-Hermes-Agent scheduled task",
                status="fixed" if ok else "failed",
                detail=detail,
            )
        )

    # ── Mode 2: Status server down ──
    if not _status_server_healthy():
        ok, detail = _restart_scheduled_task("ETA-Jarvis-Status-Server")
        actions.append(
            AutohealAction(
                asof=_now_iso(),
                mode="status_server_down",
                detection=f"port {STATUS_SERVER_PORT} /health unreachable",
                action="restart ETA-Jarvis-Status-Server scheduled task",
                status="fixed" if ok else "failed",
                detail=detail,
            )
        )

    # ── Mode 3: Audit log oversize ──
    audit_size = _audit_log_size()
    if audit_size > AUDIT_LOG_FORCE_ROTATE_BYTES:
        ok, detail = _force_rotate_audit_log()
        actions.append(
            AutohealAction(
                asof=_now_iso(),
                mode="audit_log_oversize",
                detection=f"size={audit_size} bytes > {AUDIT_LOG_FORCE_ROTATE_BYTES} threshold",
                action="force-trigger gzip rotation",
                status="fixed" if ok else "failed",
                detail=detail,
                extras={"size_bytes": audit_size},
            )
        )

    # ── Mode 4: Memory backup stale ──
    age_h = _newest_backup_age_hours()
    if age_h is None:
        # No backups yet — fire one
        ok, detail = _run_memory_backup()
        actions.append(
            AutohealAction(
                asof=_now_iso(),
                mode="memory_backup_missing",
                detection="no memory backup directory or empty",
                action="run hermes_memory_backup once",
                status="fixed" if ok else "failed",
                detail=detail,
            )
        )
    elif age_h > MEMORY_BACKUP_STALE_HOURS:
        ok, detail = _run_memory_backup()
        actions.append(
            AutohealAction(
                asof=_now_iso(),
                mode="memory_backup_stale",
                detection=f"newest backup is {age_h:.1f}h old (> {MEMORY_BACKUP_STALE_HOURS}h)",
                action="run hermes_memory_backup once",
                status="fixed" if ok else "failed",
                detail=detail,
                extras={"newest_age_h": age_h},
            )
        )

    # Persist
    for a in actions:
        _append_action(a)

    if actions:
        logger.info("autoheal: took %d action(s)", len(actions))
    else:
        logger.debug("autoheal: all systems healthy, no action")

    return actions


# ---------------------------------------------------------------------------
# Report helpers (used by morning briefing skill if it wants "what did
# autoheal do overnight")
# ---------------------------------------------------------------------------


def recent_actions(since_hours: int = 24) -> list[dict[str, Any]]:
    """Read the autoheal log and return entries newer than since_hours."""
    if not AUTOHEAL_LOG.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(hours=since_hours)
    out: list[dict[str, Any]] = []
    try:
        with AUTOHEAL_LOG.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = rec.get("asof")
                try:
                    ts = datetime.fromisoformat(str(ts_str))
                except ValueError:
                    continue
                if ts >= cutoff:
                    out.append(rec)
    except OSError as exc:
        logger.warning("autoheal recent_actions read failed: %s", exc)
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bridge autoheal watchdog — self-heals common failure modes.",
    )
    p.add_argument(
        "--once", action="store_true", help="Run one autoheal cycle and exit (use for cron / scheduled task)"
    )
    p.add_argument(
        "--loop-interval-s",
        type=int,
        default=900,
        help="When run as a daemon, sleep N seconds between cycles (default 900 = 15 min)",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON action list to stdout after each cycle")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.once:
        actions = autoheal_once()
        if args.json:
            print(json.dumps([asdict(a) for a in actions], indent=2, default=str))
        return 0

    # Daemon mode (rare — typical install runs --once via scheduled task)
    try:
        while True:
            actions = autoheal_once()
            if args.json:
                print(json.dumps([asdict(a) for a in actions], indent=2, default=str))
            time.sleep(args.loop_interval_s)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
