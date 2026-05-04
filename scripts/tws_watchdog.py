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
import json
import logging
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


_STATUS_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\tws_watchdog.json"
)


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


def _check_ib_handshake(host: str, port: int) -> tuple[bool, str]:
    """Confirm we can complete an IB API handshake (not just TCP).
    Returns (ok, detail)."""
    try:
        from ib_insync import IB
        ib = IB()
        try:
            ib.connect(host, port, clientId=55, timeout=8)
            server_version = ib.client.serverVersion() if ib.isConnected() else 0
            return ib.isConnected(), f"serverVersion={server_version}"
        finally:
            with contextlib.suppress(Exception):
                ib.disconnect()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


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
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    prior = _load_status()
    prior_failures = int(prior.get("consecutive_failures", 0))
    last_healthy_at = prior.get("last_healthy_at")

    now_iso = datetime.now(UTC).isoformat()

    socket_ok = _check_socket(args.host, args.port)
    handshake_ok, handshake_detail = (False, "skipped (socket down)")
    if socket_ok:
        handshake_ok, handshake_detail = _check_ib_handshake(args.host, args.port)

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
