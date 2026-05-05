"""TWS gateway health watchdog.

Periodic check that the IBKR Gateway is reachable on port 4002 and
that a fresh ib_insync connection succeeds. If unhealthy for >N
consecutive checks, emit a v3 event (→ Hermes Telegram) and write
status to var/eta_engine/state/tws_watchdog.json.

Designed to run on Windows Task Scheduler every 5 min. The watchdog
itself does NOT restart the gateway — TWS process management is
out of scope. It surfaces the outage so the operator knows.

Status JSON format:
    {
      "checked_at": "...iso...",
      "healthy": true/false,
      "consecutive_failures": int,
      "last_healthy_at": "...iso...",
      "details": {...}
    }
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import logging
import os
import re
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


_STATUS_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\tws_watchdog.json"
)
_DEFAULT_CRASH_LOG_DIR = Path(r"C:\Jts\ibgateway\1046")
_DEFAULT_WATCHDOG_CLIENT_IDS = (55, 99, 101, 102)


def _bootstrap_env() -> None:
    env_path = Path(r"C:\EvolutionaryTradingAlgo\eta_engine\.env")
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_bootstrap_env()


def _check_socket(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _watchdog_client_ids() -> tuple[int, ...]:
    raw = os.environ.get("ETA_TWS_WATCHDOG_CLIENT_IDS", "")
    if not raw:
        return _DEFAULT_WATCHDOG_CLIENT_IDS
    ids: list[int] = []
    for chunk in raw.split(","):
        with contextlib.suppress(ValueError):
            ids.append(int(chunk.strip()))
    return tuple(ids) or _DEFAULT_WATCHDOG_CLIENT_IDS


def _check_ib_handshake(
    host: str,
    port: int,
    *,
    attempts: int = 2,
    timeout: float = 12.0,
) -> tuple[bool, str]:
    """Confirm we can complete an IB API handshake (not just TCP).
    Returns (ok, detail)."""
    details: list[str] = []
    client_ids = _watchdog_client_ids()
    for attempt in range(1, max(1, attempts) + 1):
        client_id = client_ids[(attempt - 1) % len(client_ids)]
        try:
            from ib_insync import IB
            ib = IB()
            try:
                ib.connect(host, port, clientId=client_id, timeout=timeout)
                server_version = ib.client.serverVersion() if ib.isConnected() else 0
                if ib.isConnected():
                    return True, (
                        f"serverVersion={server_version}; clientId={client_id}; "
                        f"attempt={attempt}"
                    )
                details.append(f"attempt {attempt} clientId={client_id}: not connected")
            finally:
                with contextlib.suppress(Exception):
                    ib.disconnect()
        except Exception as exc:  # noqa: BLE001
            details.append(f"attempt {attempt} clientId={client_id}: {type(exc).__name__}({exc})")
        if attempt < attempts:
            time.sleep(2)
    return False, "; ".join(details)


def _latest_gateway_crash(crash_log_dir: Path) -> dict | None:
    """Summarize the newest IB Gateway JVM crash log, if one exists."""
    try:
        candidates = [
            path for path in crash_log_dir.glob("hs_err_pid*.log")
            if path.is_file()
        ]
    except OSError:
        return None
    if not candidates:
        return None
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        text = "\n".join(latest.read_text(encoding="utf-8", errors="replace").splitlines()[:80])
    except OSError:
        return None

    insufficient_memory = "insufficient memory" in text.lower()
    native_allocation = next(
        (line.strip("# ").strip() for line in text.splitlines() if "Native memory allocation" in line),
        "",
    )
    xmx_match = re.search(r"-Xmx(\d+[mMgG])", text)
    if insufficient_memory or native_allocation:
        reason_code = "jvm_native_memory_oom"
        summary = "IB Gateway JVM native-memory OOM"
    else:
        reason_code = "jvm_crash"
        summary = "IB Gateway JVM crash"
    return {
        "reason_code": reason_code,
        "summary": summary,
        "path": str(latest),
        "mtime": datetime.fromtimestamp(latest.stat().st_mtime, UTC).isoformat(),
        "native_allocation": native_allocation,
        "xmx": xmx_match.group(1).lower() if xmx_match else None,
    }


def _gateway_process_snapshot(gateway_dir: Path) -> dict | None:
    """Return the live IB Gateway process, if Windows can see one."""
    if os.name != "nt":
        return None
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ibgateway.exe", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout.strip()
    if completed.returncode != 0 or not output:
        return None
    try:
        rows = list(csv.reader(output.splitlines()))
    except csv.Error:
        return None
    if not rows or rows[0][0].lower().startswith("info:"):
        return None
    row = rows[0]
    if len(row) < 5:
        return None
    memory_kb = float(re.sub(r"[^\d.]", "", row[4]) or 0)
    return {
        "running": True,
        "pid": int(row[1]),
        "name": row[0],
        "working_set_mb": round(memory_kb / 1024, 1),
        "gateway_dir": str(gateway_dir),
    }


def _load_status() -> dict:
    if not _STATUS_PATH.exists():
        return {}
    try:
        return json.loads(_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_status(status: dict) -> None:
    with contextlib.suppress(OSError):
        _STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATUS_PATH.write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--alert-after", type=int, default=2,
        help="Consecutive failures before emitting a v3 alert (default 2 = 10 min at 5-min cadence).",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002)
    p.add_argument(
        "--handshake-attempts",
        type=int,
        default=2,
        help="IB API handshake attempts before classifying the gateway as unhealthy.",
    )
    p.add_argument(
        "--handshake-timeout",
        type=float,
        default=12.0,
        help="Seconds to wait for each IB API handshake attempt.",
    )
    p.add_argument(
        "--crash-log-dir",
        default=os.environ.get("ETA_TWS_CRASH_LOG_DIR", str(_DEFAULT_CRASH_LOG_DIR)),
        help="Directory containing IB Gateway hs_err_pid*.log crash artifacts.",
    )
    p.add_argument(
        "--gateway-dir",
        default=os.environ.get("ETA_TWS_GATEWAY_DIR", str(_DEFAULT_CRASH_LOG_DIR)),
        help="IB Gateway installation directory used for process diagnostics.",
    )
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    prior = _load_status()
    prior_failures = int(prior.get("consecutive_failures", 0))
    last_healthy_at = prior.get("last_healthy_at")

    now_iso = datetime.now(UTC).isoformat()

    handshake_ok, handshake_detail = _check_ib_handshake(
        args.host,
        args.port,
        attempts=args.handshake_attempts,
        timeout=args.handshake_timeout,
    )
    socket_ok = True if handshake_ok else _check_socket(args.host, args.port)

    healthy = socket_ok and handshake_ok
    if healthy:
        consecutive_failures = 0
        last_healthy_at = now_iso
        logger.info("TWS healthy at %s:%d (%s)", args.host, args.port, handshake_detail)
    else:
        consecutive_failures = prior_failures + 1
        logger.warning(
            "TWS UNHEALTHY socket=%s handshake=%s (%s) — fail #%d",
            socket_ok, handshake_ok, handshake_detail, consecutive_failures,
        )

    status = {
        "checked_at": now_iso,
        "healthy": healthy,
        "consecutive_failures": consecutive_failures,
        "last_healthy_at": last_healthy_at,
        "details": {
            "host": args.host,
            "port": args.port,
            "socket_ok": socket_ok,
            "handshake_ok": handshake_ok,
            "handshake_detail": handshake_detail,
        },
    }
    gateway_crash = None if healthy else _latest_gateway_crash(Path(args.crash_log_dir))
    if gateway_crash is not None:
        status["details"]["gateway_crash"] = gateway_crash
    gateway_process = None if healthy else _gateway_process_snapshot(Path(args.gateway_dir))
    if gateway_process is not None:
        status["details"]["gateway_process"] = gateway_process
    _save_status(status)

    # Alert when we cross the threshold (only on the EDGE — N-th failure
    # in a row — not every subsequent failure, otherwise we'd spam).
    if not healthy and consecutive_failures == args.alert_after:
        try:
            from eta_engine.brain.jarvis_v3.policies._v3_events import emit_event
            emit_event(
                layer="ops",
                event="tws_gateway_unhealthy",
                bot_id="",
                cls="",
                details={
                    "consecutive_failures": consecutive_failures,
                    "host": args.host,
                    "port": args.port,
                    "handshake_detail": handshake_detail,
                    "gateway_crash": gateway_crash,
                    "gateway_process": gateway_process,
                    "last_healthy_at": last_healthy_at,
                },
                severity="CRITICAL",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("v3 event emit failed: %s", exc)
    elif healthy and prior_failures >= args.alert_after:
        # Recovery edge — let the operator know the gateway came back.
        try:
            from eta_engine.brain.jarvis_v3.policies._v3_events import emit_event
            emit_event(
                layer="ops",
                event="tws_gateway_recovered",
                bot_id="",
                cls="",
                details={
                    "after_failures": prior_failures,
                    "handshake_detail": handshake_detail,
                },
                severity="INFO",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("v3 event emit failed: %s", exc)

    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
