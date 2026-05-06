"""Drift Alarm Alerter — polls ``/api/live/per_bot_alpaca`` and pings Telegram.

Polls the canonical ETA dashboard API every ``ETA_DRIFT_ALERT_INTERVAL_S``
seconds (default 300 = 5 min). For each bot whose snapshot reports
``drift_alarm: true`` and which has not been alerted on in the last
``ETA_DRIFT_ALERT_DEDUP_S`` seconds (default 3600 = 1 h), it dispatches a
Telegram alert via the existing :mod:`eta_engine.brain.jarvis_v3.hermes_bridge`
``send_alert`` coroutine. As a secondary path, if
``ETA_TELEGRAM_WEBHOOK_URL`` is set the same JSON payload is POSTed there
(useful for Slack/n8n/Hermes-mirror redirection).

State persists at ``var/eta_engine/state/drift_alert_state.json`` keyed by
``bot_id`` with the last-alerted UTC timestamp + an ``updated_at`` heartbeat
that the operator can grep to confirm the alerter is alive. Writes go via
``.tmp + os.replace`` so an interrupted write can never corrupt the file.

Fail-soft: HTTP errors hitting the dashboard, the Telegram API, or the
optional webhook are logged and swallowed. The poll loop only ever exits
on SIGINT / SIGTERM.

Run as a Windows scheduled task (see
``deploy/scripts/install_eta_drift_alerter.ps1``) — the script is also
runnable standalone for debugging:

    python -m eta_engine.scripts.drift_alarm_alerter --once
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("drift_alarm_alerter")

# ---- canonical paths (CLAUDE.md hard rule #1: workspace-only writes) ----
_WORKSPACE_ROOT = Path(
    os.environ.get("ETA_WORKSPACE_ROOT", r"C:\EvolutionaryTradingAlgo")
)
_STATE_DIR = _WORKSPACE_ROOT / "var" / "eta_engine" / "state"
_STATE_FILE = _STATE_DIR / "drift_alert_state.json"

# ---- tunables ----
_DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8000/api/live/per_bot_alpaca"
_DEFAULT_INTERVAL_S = 300  # 5 min poll
_DEFAULT_DEDUP_S = 3600  # 1 h re-alert window
_HTTP_TIMEOUT_S = 10.0


def _dashboard_url() -> str:
    return os.environ.get("ETA_DRIFT_ALERT_URL", _DEFAULT_DASHBOARD_URL)


def _interval_s() -> int:
    raw = os.environ.get("ETA_DRIFT_ALERT_INTERVAL_S", str(_DEFAULT_INTERVAL_S))
    try:
        return max(10, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_S


def _dedup_s() -> int:
    raw = os.environ.get("ETA_DRIFT_ALERT_DEDUP_S", str(_DEFAULT_DEDUP_S))
    try:
        return max(60, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_DEDUP_S


def _webhook_url() -> str:
    return os.environ.get("ETA_TELEGRAM_WEBHOOK_URL", "").strip()


# ---- state I/O ----
def load_state(path: Path = _STATE_FILE) -> dict[str, Any]:
    """Read the dedup state file. Returns ``{}`` if missing/corrupt."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("drift_alert state read failed (%s); starting clean", exc)
    return {}


def save_state(state: dict[str, Any], path: Path = _STATE_FILE) -> None:
    """Atomically write the dedup state. Creates parent dirs if missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(state, indent=2, sort_keys=True)
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp, path)


# ---- dashboard fetch ----
def fetch_per_bot_snapshot(url: str | None = None, *, timeout: float = _HTTP_TIMEOUT_S) -> dict[str, Any]:
    """Fetch the per-bot Alpaca snapshot. Returns ``{}`` on any failure."""
    target = url or _dashboard_url()
    try:
        import httpx  # local import keeps the module importable in test envs without httpx

        with httpx.Client(timeout=timeout) as client:
            r = client.get(target)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001 — fail-soft
        logger.warning("dashboard fetch failed: %s", exc)
    return {}


# ---- alert dispatch ----
def _format_message(bot_id: str, info: dict[str, Any]) -> tuple[str, str]:
    """Compose ``(title, body)`` for a drift-alarm Telegram message."""
    title = f"Drift alarm: {bot_id}"
    drift_pp = info.get("drift_gap_pp")
    live_wr = info.get("live_wr_today")
    bt_wr = info.get("backtest_wr_target")
    wins = info.get("wins")
    losses = info.get("losses")
    parts: list[str] = []
    if isinstance(drift_pp, (int, float)):
        parts.append(f"drift={drift_pp:.1f}pp")
    if isinstance(live_wr, (int, float)):
        parts.append(f"live_wr={live_wr:.1%}")
    if isinstance(bt_wr, (int, float)):
        parts.append(f"backtest_wr={bt_wr:.1%}")
    if isinstance(wins, int) and isinstance(losses, int):
        parts.append(f"w/l={wins}/{losses}")
    body = ", ".join(parts) if parts else "(no detail fields)"
    return title, body


def _send_via_hermes(title: str, body: str) -> bool:
    """Use the existing eta_engine hermes_bridge.send_alert path. Async-safe."""
    try:
        from eta_engine.brain.jarvis_v3.hermes_bridge import send_alert
    except ImportError as exc:
        logger.debug("hermes_bridge unavailable: %s", exc)
        return False
    try:
        return bool(asyncio.run(send_alert(title, body, level="WARN")))
    except Exception as exc:  # noqa: BLE001 — fail-soft
        logger.warning("hermes send_alert failed: %s", exc)
        return False


def _send_via_webhook(payload: dict[str, Any]) -> bool:
    """POST the alert to the operator-configured webhook (Slack/n8n/etc.)."""
    url = _webhook_url()
    if not url:
        return False
    try:
        import httpx

        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            r = client.post(url, json=payload)
            return 200 <= r.status_code < 300
    except Exception as exc:  # noqa: BLE001 — fail-soft
        logger.warning("webhook POST failed: %s", exc)
        return False


def dispatch_alert(bot_id: str, info: dict[str, Any]) -> bool:
    """Send a drift alarm via Hermes + (optionally) the webhook mirror.

    Returns True if at least one transport reported success.
    """
    title, body = _format_message(bot_id, info)
    payload = {
        "title": title,
        "body": body,
        "bot_id": bot_id,
        "level": "WARN",
        "kind": "drift_alarm",
        "details": {
            k: info.get(k)
            for k in ("drift_gap_pp", "live_wr_today", "backtest_wr_target", "wins", "losses", "fills_today")
            if k in info
        },
        "ts_utc": datetime.now(UTC).isoformat(),
    }
    sent_any = False
    if _send_via_hermes(title, body):
        sent_any = True
    if _send_via_webhook(payload):
        sent_any = True
    if not sent_any:
        logger.warning("drift_alarm for %s: NO transport succeeded", bot_id)
    return sent_any


# ---- core poll/dedup ----
def _now_ts() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def select_bots_to_alert(
    snapshot: dict[str, Any],
    state: dict[str, Any],
    *,
    dedup_s: int,
    now_ts: float | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Return list of ``(bot_id, info)`` that need an alert right now.

    Skips bots whose previous alert is within ``dedup_s`` seconds.
    """
    if now_ts is None:
        now_ts = _now_ts()
    out: list[tuple[str, dict[str, Any]]] = []
    per_bot = snapshot.get("per_bot") or {}
    if not isinstance(per_bot, dict):
        return out
    bots = state.setdefault("bots", {})
    for bot_id, info in per_bot.items():
        if not isinstance(info, dict):
            continue
        if not info.get("drift_alarm"):
            continue
        prev = bots.get(bot_id) or {}
        last = prev.get("last_alert_ts") if isinstance(prev, dict) else None
        if isinstance(last, (int, float)) and (now_ts - float(last)) < dedup_s:
            continue
        out.append((bot_id, info))
    return out


def run_once(
    *,
    state_path: Path = _STATE_FILE,
    dashboard_url: str | None = None,
    dedup_s: int | None = None,
    now_ts: float | None = None,
    fetcher: "object | None" = None,
    dispatcher: "object | None" = None,
) -> dict[str, Any]:
    """Single poll cycle. Returns a heartbeat-style summary dict."""
    if dedup_s is None:
        dedup_s = _dedup_s()
    fetch_fn = fetcher if fetcher is not None else fetch_per_bot_snapshot
    dispatch_fn = dispatcher if dispatcher is not None else dispatch_alert
    state = load_state(state_path)
    snapshot = fetch_fn(dashboard_url) if dashboard_url is not None else fetch_fn()
    bots_to_alert = select_bots_to_alert(snapshot, state, dedup_s=dedup_s, now_ts=now_ts)
    alerted: list[str] = []
    for bot_id, info in bots_to_alert:
        sent = False
        try:
            sent = bool(dispatch_fn(bot_id, info))
        except Exception as exc:  # noqa: BLE001 — fail-soft
            logger.warning("dispatch_alert raised for %s: %s", bot_id, exc)
            sent = False
        if sent:
            alerted.append(bot_id)
            state.setdefault("bots", {})[bot_id] = {
                "last_alert_ts": float(now_ts) if now_ts is not None else _now_ts(),
                "last_alert_iso": _now_iso(),
                "drift_gap_pp": info.get("drift_gap_pp"),
            }
    state["updated_at"] = _now_iso()
    state["last_poll_ok"] = bool(snapshot.get("ready") or snapshot.get("per_bot"))
    state["last_poll_ts"] = float(now_ts) if now_ts is not None else _now_ts()
    state["last_alerted"] = alerted
    save_state(state, state_path)
    return {
        "alerted": alerted,
        "considered": [b for b, _ in bots_to_alert],
        "ready": bool(snapshot.get("ready")),
    }


def run_forever(
    *,
    state_path: Path = _STATE_FILE,
    dashboard_url: str | None = None,
    interval_s: int | None = None,
    dedup_s: int | None = None,
) -> None:
    """Long-running poll loop. Survives all per-iteration exceptions."""
    interval = interval_s if interval_s is not None else _interval_s()
    dedup = dedup_s if dedup_s is not None else _dedup_s()
    stop_flag = {"stop": False}

    def _stop(*_args: Any) -> None:
        stop_flag["stop"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    logger.info(
        "drift_alerter starting | url=%s interval=%ss dedup=%ss state=%s",
        dashboard_url or _dashboard_url(),
        interval,
        dedup,
        state_path,
    )
    while not stop_flag["stop"]:
        try:
            summary = run_once(
                state_path=state_path,
                dashboard_url=dashboard_url,
                dedup_s=dedup,
            )
            if summary["alerted"]:
                logger.info("drift_alerter dispatched: %s", summary["alerted"])
        except Exception as exc:  # noqa: BLE001 — never crash the loop
            logger.warning("drift_alerter iteration failed: %s", exc)
        for _ in range(interval):
            if stop_flag["stop"]:
                break
            time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run a single poll cycle then exit (debugging)")
    parser.add_argument("--url", default=None, help="Dashboard endpoint URL (default $ETA_DRIFT_ALERT_URL)")
    parser.add_argument("--interval", type=int, default=None, help="Poll interval seconds (default $ETA_DRIFT_ALERT_INTERVAL_S=300)")
    parser.add_argument("--dedup", type=int, default=None, help="Dedup window seconds (default $ETA_DRIFT_ALERT_DEDUP_S=3600)")
    parser.add_argument("--state", default=None, help="State file path (default var/eta_engine/state/drift_alert_state.json)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    state_path = Path(args.state) if args.state else _STATE_FILE
    if args.once:
        summary = run_once(state_path=state_path, dashboard_url=args.url, dedup_s=args.dedup)
        logger.info("once: %s", summary)
        return 0
    run_forever(state_path=state_path, dashboard_url=args.url, interval_s=args.interval, dedup_s=args.dedup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
