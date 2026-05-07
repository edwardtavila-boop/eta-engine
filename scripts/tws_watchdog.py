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
import asyncio
import contextlib
import csv
import json
import logging
import os
import re
import socket
import subprocess
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.scripts import ibgateway_reauth_controller

logger = logging.getLogger(__name__)


_STATUS_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\tws_watchdog.json"
)
_DEFAULT_CRASH_LOG_DIR = Path(r"C:\Jts\ibgateway\1046")
_DEFAULT_WATCHDOG_CLIENT_IDS = (55, 99, 101, 102)
_GATEWAY_PROCESS_NAMES = ("ibgateway.exe", "ibgateway1.exe")
_LAST_ACCOUNT_SNAPSHOT: dict | None = None


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


def _default_handshake_timeout() -> float:
    raw = (
        os.environ.get("ETA_TWS_WATCHDOG_HANDSHAKE_TIMEOUT_S", "").strip()
        or os.environ.get("ETA_IBKR_CONNECT_TIMEOUT_S", "").strip()
        or "45"
    )
    try:
        timeout = float(raw)
    except ValueError:
        logger.warning(
            "watchdog handshake timeout %r is invalid; using 45.0 seconds",
            raw,
        )
        return 45.0
    if timeout <= 0:
        logger.warning(
            "watchdog handshake timeout %r must be > 0; using 45.0 seconds",
            raw,
        )
        return 45.0
    return timeout


def _ensure_asyncio_event_loop() -> None:
    """ib_insync still expects a default loop on Python versions that no longer create one."""
    loop: asyncio.AbstractEventLoop | None = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            loop = asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
        return
    if loop is not None and loop.is_closed():
        asyncio.set_event_loop(asyncio.new_event_loop())


def _mask_account(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= 4:
        return "***"
    return f"{raw[:3]}...{raw[-4:]}"


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_or_text(value: object) -> str:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        with contextlib.suppress(Exception):
            return str(isoformat())
    return str(value or "")


def _contract_snapshot(contract: object) -> dict:
    return {
        "symbol": str(getattr(contract, "symbol", "") or ""),
        "sec_type": str(getattr(contract, "secType", "") or ""),
        "exchange": str(getattr(contract, "exchange", "") or ""),
        "currency": str(getattr(contract, "currency", "") or ""),
        "local_symbol": str(getattr(contract, "localSymbol", "") or ""),
        "con_id": getattr(contract, "conId", None),
    }


def _position_snapshot(position: object) -> dict:
    return {
        "account": _mask_account(getattr(position, "account", "")),
        "contract": _contract_snapshot(getattr(position, "contract", None)),
        "position": _float_or_none(getattr(position, "position", None)),
        "avg_cost": _float_or_none(getattr(position, "avgCost", None)),
    }


def _portfolio_snapshot(item: object) -> dict:
    return {
        "account": _mask_account(getattr(item, "account", "")),
        "contract": _contract_snapshot(getattr(item, "contract", None)),
        "position": _float_or_none(getattr(item, "position", None)),
        "market_price": _float_or_none(getattr(item, "marketPrice", None)),
        "market_value": _float_or_none(getattr(item, "marketValue", None)),
        "average_cost": _float_or_none(getattr(item, "averageCost", None)),
        "unrealized_pnl": _float_or_none(getattr(item, "unrealizedPNL", None)),
        "realized_pnl": _float_or_none(getattr(item, "realizedPNL", None)),
    }


def _execution_snapshot(fill: object) -> dict:
    contract = getattr(fill, "contract", None)
    execution = getattr(fill, "execution", None)
    commission = getattr(fill, "commissionReport", None)
    order_ref = str(getattr(execution, "orderRef", "") or "")
    row = {
        "ts": _iso_or_text(getattr(execution, "time", "")),
        "account": _mask_account(getattr(execution, "acctNumber", "")),
        "symbol": str(getattr(contract, "symbol", "") or ""),
        "local_symbol": str(getattr(contract, "localSymbol", "") or ""),
        "sec_type": str(getattr(contract, "secType", "") or ""),
        "exchange": str(getattr(contract, "exchange", "") or ""),
        "side": str(getattr(execution, "side", "") or ""),
        "qty": _float_or_none(getattr(execution, "shares", None)),
        "price": _float_or_none(getattr(execution, "price", None)),
        "order_id": getattr(execution, "orderId", None),
        "perm_id": getattr(execution, "permId", None),
        "exec_id": str(getattr(execution, "execId", "") or ""),
        "order_ref": order_ref,
        "bot": order_ref,
        "source": "ibkr_execution",
    }
    if commission is not None:
        row["commission"] = _float_or_none(getattr(commission, "commission", None))
        row["commission_currency"] = str(getattr(commission, "currency", "") or "")
        row["realized_pnl"] = _float_or_none(getattr(commission, "realizedPNL", None))
    return row


def _snapshot_from_ib(ib: object, *, execution_limit: int = 50) -> dict:
    """Capture sanitized account truth from an already-connected IB object."""
    positions = [_position_snapshot(item) for item in list(ib.positions() or [])]
    portfolio = [_portfolio_snapshot(item) for item in list(ib.portfolio() or [])]
    fills = []
    try:
        fills = list(ib.reqExecutions() or [])
    except Exception:  # noqa: BLE001 - snapshot enrichments must not fail health.
        with contextlib.suppress(Exception):
            fills = list(ib.fills() or [])
    executions = [_execution_snapshot(item) for item in fills]
    executions.sort(key=lambda row: str(row.get("ts") or ""))
    if execution_limit > 0:
        executions = executions[-execution_limit:]
    accounts = sorted(
        {
            str(row.get("account"))
            for row in [*positions, *portfolio, *executions]
            if row.get("account")
        }
    )
    open_positions = [
        row for row in positions
        if abs(float(row.get("position") or 0.0)) > 0
    ]
    last_execution = executions[-1] if executions else {}
    realized_values = [
        float(row["realized_pnl"])
        for row in executions
        if row.get("realized_pnl") is not None
    ]
    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "accounts": accounts,
        "summary": {
            "accounts": accounts,
            "positions_count": len(positions),
            "open_positions_count": len(open_positions),
            "portfolio_count": len(portfolio),
            "executions_count": len(executions),
            "last_execution_ts": last_execution.get("ts"),
            "last_execution_symbol": last_execution.get("symbol"),
            "last_execution_side": last_execution.get("side"),
            "last_execution_qty": last_execution.get("qty"),
            "last_execution_price": last_execution.get("price"),
            "realized_pnl": round(sum(realized_values), 2) if realized_values else None,
        },
        "positions": positions,
        "portfolio": portfolio,
        "executions": executions,
    }


def _check_ib_handshake(
    host: str,
    port: int,
    *,
    attempts: int = 2,
    timeout: float | None = None,
) -> tuple[bool, str]:
    """Confirm we can complete an IB API handshake (not just TCP).
    Returns (ok, detail)."""
    global _LAST_ACCOUNT_SNAPSHOT
    _LAST_ACCOUNT_SNAPSHOT = None
    timeout = _default_handshake_timeout() if timeout is None else timeout
    details: list[str] = []
    client_ids = _watchdog_client_ids()
    for attempt in range(1, max(1, attempts) + 1):
        client_id = client_ids[(attempt - 1) % len(client_ids)]
        try:
            _ensure_asyncio_event_loop()
            from ib_insync import IB
            ib = IB()
            try:
                ib.connect(
                    host,
                    port,
                    clientId=client_id,
                    timeout=timeout,
                    readonly=True,
                )
                server_version = ib.client.serverVersion() if ib.isConnected() else 0
                if ib.isConnected():
                    with contextlib.suppress(Exception):
                        _LAST_ACCOUNT_SNAPSHOT = _snapshot_from_ib(ib)
                    summary = (
                        _LAST_ACCOUNT_SNAPSHOT.get("summary")
                        if isinstance(_LAST_ACCOUNT_SNAPSHOT, dict)
                        else {}
                    )
                    activity_detail = ""
                    if isinstance(summary, dict):
                        activity_detail = (
                            f"; positions={summary.get('open_positions_count', 0)} open"
                            f"; executions={summary.get('executions_count', 0)}"
                        )
                    return True, (
                        f"serverVersion={server_version}; clientId={client_id}; "
                        f"attempt={attempt}{activity_detail}"
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
    not_running = {
        "running": False,
        "gateway_dir": str(gateway_dir),
        "name": "/".join(_GATEWAY_PROCESS_NAMES),
    }
    for process_name in _GATEWAY_PROCESS_NAMES:
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        output = completed.stdout.strip()
        if completed.returncode != 0 or not output:
            continue
        try:
            rows = list(csv.reader(output.splitlines()))
        except csv.Error:
            return None
        if not rows or rows[0][0].lower().startswith("info:"):
            continue
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
    return not_running


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


def _recovery_lane_snapshot(*, healthy: bool) -> dict:
    lane = ibgateway_reauth_controller.recovery_lane_metadata(
        tws_status_path=_STATUS_PATH,
        state_path=ibgateway_reauth_controller.DEFAULT_REAUTH_STATE_PATH,
        run_now_task=ibgateway_reauth_controller.RUN_NOW_TASK_NAME,
        restart_task=ibgateway_reauth_controller.RESTART_TASK_NAME,
    )
    lane["action_owner"] = lane["controller_task"]
    lane["operator_action"] = "" if healthy else (
        f"Inspect {lane['state_path']} or start {lane['controller_task']} after clearing any IBKR login or 2FA prompt."
    )
    return lane


def main(argv: list[str] | None = None) -> int:
    global _LAST_ACCOUNT_SNAPSHOT
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
        default=_default_handshake_timeout(),
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

    _LAST_ACCOUNT_SNAPSHOT = None
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
        "recovery_lane": _recovery_lane_snapshot(healthy=healthy),
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
    if healthy and _LAST_ACCOUNT_SNAPSHOT is not None:
        status["details"]["account_snapshot"] = _LAST_ACCOUNT_SNAPSHOT
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
