"""Refresh broker-vs-supervisor position reconciliation.

This is a read-only VPS heartbeat. It compares current IBKR broker
positions with the supervisor heartbeat and refreshes the canonical
``reconcile_last.json`` artifact that the hardening audit reads.

It never submits, cancels, flattens, promotes, or acknowledges orders.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eta_engine.scripts import workspace_roots  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_OUT = workspace_roots.ETA_JARVIS_SUPERVISOR_RECONCILE_PATH
DEFAULT_STATUS_OUT = workspace_roots.ETA_RUNTIME_STATE_DIR / "supervisor_broker_reconcile_heartbeat.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4002
DEFAULT_CLIENT_ID = 78


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _symbol_root(symbol: object) -> str:
    raw = str(symbol or "").upper().strip()
    if not raw:
        return ""
    return raw.rstrip("0123456789").replace("USDT", "").replace("USD", "")


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return default


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_supervisor_positions(heartbeat_path: Path = workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH) -> dict[str, Any]:
    heartbeat = _read_json(heartbeat_path)
    rows: list[dict[str, Any]] = []
    for bot in heartbeat.get("bots", []) if isinstance(heartbeat.get("bots"), list) else []:
        if not isinstance(bot, dict):
            continue
        pos = bot.get("open_position")
        if not isinstance(pos, dict):
            continue
        root = _symbol_root(bot.get("symbol"))
        qty = abs(_float(pos.get("qty")))
        side = str(pos.get("side") or "BUY").upper()
        signed_qty = qty if side == "BUY" else -qty
        if not root or abs(signed_qty) < 1e-9:
            continue
        rows.append(
            {
                "bot_id": bot.get("bot_id"),
                "symbol": bot.get("symbol"),
                "root": root,
                "side": side,
                "qty": qty,
                "signed_qty": signed_qty,
                "entry_price": pos.get("entry_price"),
                "signal_id": pos.get("signal_id"),
            }
        )
    return {
        "path": str(heartbeat_path),
        "heartbeat_ts": heartbeat.get("ts"),
        "mode": heartbeat.get("mode"),
        "rows": rows,
    }


def query_ibkr_positions(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    client_id: int = DEFAULT_CLIENT_ID,
    timeout_s: float = 10.0,
) -> list[dict[str, Any]]:
    """Query current IBKR positions using a dedicated read-only client id."""

    logging.getLogger("ib_insync").setLevel(logging.WARNING)
    from ib_insync import IB

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=timeout_s)
        rows: list[dict[str, Any]] = []
        for position in ib.positions():
            contract = position.contract
            root = _symbol_root(getattr(contract, "symbol", ""))
            qty = _float(getattr(position, "position", 0.0))
            if not root or abs(qty) < 1e-9:
                continue
            rows.append(
                {
                    "account": getattr(position, "account", ""),
                    "symbol": getattr(contract, "symbol", ""),
                    "local_symbol": getattr(contract, "localSymbol", ""),
                    "root": root,
                    "sec_type": getattr(contract, "secType", ""),
                    "exchange": getattr(contract, "exchange", "") or getattr(contract, "primaryExchange", ""),
                    "currency": getattr(contract, "currency", ""),
                    "position": qty,
                    "avg_cost": _float(getattr(position, "avgCost", 0.0)),
                }
            )
        return rows
    finally:
        with contextlib.suppress(Exception):
            ib.disconnect()


def _sum_by_root(rows: list[dict[str, Any]], qty_key: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        root = _symbol_root(row.get("root") or row.get("symbol"))
        if not root:
            continue
        out[root] = out.get(root, 0.0) + _float(row.get(qty_key))
    return out


def build_reconcile_snapshot(
    *,
    supervisor: dict[str, Any],
    broker_rows: list[dict[str, Any]],
    checked_at: datetime | None = None,
    path: Path = DEFAULT_OUT,
) -> dict[str, Any]:
    checked = checked_at or _utc_now()
    supervisor_rows = supervisor.get("rows") if isinstance(supervisor.get("rows"), list) else []
    supervisor_by_root = _sum_by_root(supervisor_rows, "signed_qty")
    broker_by_root = _sum_by_root(broker_rows, "position")

    broker_only: list[dict[str, Any]] = []
    supervisor_only: list[dict[str, Any]] = []
    divergent: list[dict[str, Any]] = []
    matched_positions: list[dict[str, Any]] = []

    for root in sorted(set(broker_by_root) | set(supervisor_by_root)):
        broker_qty = broker_by_root.get(root, 0.0)
        supervisor_qty = supervisor_by_root.get(root, 0.0)
        delta = broker_qty - supervisor_qty
        if abs(delta) <= 1e-6:
            matched_positions.append(
                {
                    "symbol": root,
                    "broker_qty": broker_qty,
                    "supervisor_qty": supervisor_qty,
                }
            )
        elif abs(supervisor_qty) <= 1e-6:
            broker_only.append({"symbol": root, "broker_qty": broker_qty})
        elif abs(broker_qty) <= 1e-6:
            supervisor_only.append({"symbol": root, "supervisor_qty": supervisor_qty})
        else:
            row: dict[str, Any] = {
                "symbol": root,
                "broker_qty": broker_qty,
                "supervisor_qty": supervisor_qty,
                "delta": delta,
            }
            if abs(delta) > 1e-6:
                row["broker_excess_action"] = "SELL" if delta > 0 else "BUY"
                row["broker_excess_qty"] = abs(delta)
            divergent.append(row)

    action_plan: list[dict[str, Any]] = []
    for row in broker_only:
        qty = _float(row.get("broker_qty"))
        action_plan.append(
            {
                "symbol": row.get("symbol"),
                "category": "broker_only",
                "safe_default": "hold_new_entries",
                "operator_review": "broker has exposure supervisor does not claim; flatten or adopt deliberately",
                "candidate_flatten_action": "SELL" if qty > 0 else "BUY",
                "candidate_flatten_qty": abs(qty),
            }
        )
    for row in divergent:
        action_plan.append(
            {
                "symbol": row.get("symbol"),
                "category": "divergent",
                "safe_default": "hold_new_entries",
                "operator_review": "broker and supervisor disagree on size; reduce excess or repair supervisor state deliberately",
                "candidate_excess_action": row.get("broker_excess_action"),
                "candidate_excess_qty": row.get("broker_excess_qty"),
            }
        )
    for row in supervisor_only:
        action_plan.append(
            {
                "symbol": row.get("symbol"),
                "category": "supervisor_only",
                "safe_default": "hold_new_entries",
                "operator_review": "supervisor claims exposure broker does not show; clear stale supervisor file only after broker flat is confirmed",
            }
        )

    return {
        "checked_at": checked.isoformat(),
        "generated_at_utc": checked.isoformat(),
        "mode": supervisor.get("mode"),
        "source": "supervisor_broker_reconcile_heartbeat",
        "path": str(path),
        "supervisor_heartbeat_path": supervisor.get("path"),
        "supervisor_heartbeat_ts": supervisor.get("heartbeat_ts"),
        "broker_only": broker_only,
        "supervisor_only": supervisor_only,
        "divergent": divergent,
        "matched": len(matched_positions),
        "matched_positions": matched_positions,
        "mismatch_count": len(broker_only) + len(supervisor_only) + len(divergent),
        "brokers_queried": ["ibkr"],
        "broker_positions": broker_rows,
        "supervisor_positions": supervisor_rows,
        "action_plan": action_plan,
        "order_action_allowed": False,
    }


def build_status(
    *,
    ok: bool,
    out: Path,
    status_out: Path,
    snapshot: dict[str, Any] | None = None,
    error: str = "",
    started_at: datetime | None = None,
) -> dict[str, Any]:
    generated = _utc_now()
    snapshot = snapshot or {}
    elapsed_ms = None
    if started_at is not None:
        elapsed_ms = round((generated - started_at).total_seconds() * 1000, 1)
    return {
        "ok": ok,
        "status": "fresh" if ok else "error",
        "source": "supervisor_broker_reconcile_heartbeat",
        "generated_at_utc": generated.isoformat(),
        "elapsed_ms": elapsed_ms,
        "out": str(out),
        "status_out": str(status_out),
        "error": error,
        "mismatch_count": int(snapshot.get("mismatch_count") or 0),
        "broker_only_symbols": [str(row.get("symbol")) for row in snapshot.get("broker_only", [])],
        "supervisor_only_symbols": [str(row.get("symbol")) for row in snapshot.get("supervisor_only", [])],
        "divergent_symbols": [str(row.get("symbol")) for row in snapshot.get("divergent", [])],
        "order_action_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--status-out", type=Path, default=DEFAULT_STATUS_OUT)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID)
    parser.add_argument("--timeout-s", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", help="Print machine-readable status.")
    parser.add_argument("--no-write", action="store_true", help="Do not write artifacts.")
    args = parser.parse_args(argv)

    started_at = _utc_now()
    try:
        supervisor = load_supervisor_positions()
        broker_rows = query_ibkr_positions(
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            timeout_s=args.timeout_s,
        )
        snapshot = build_reconcile_snapshot(supervisor=supervisor, broker_rows=broker_rows, path=args.out)
        status = build_status(
            ok=True,
            out=args.out,
            status_out=args.status_out,
            snapshot=snapshot,
            started_at=started_at,
        )
        if not args.no_write:
            _write_json_atomic(args.out, snapshot)
            _write_json_atomic(args.status_out, status)
        if args.json:
            print(json.dumps(status, indent=2, sort_keys=True))
        else:
            logger.info("supervisor/broker reconcile mismatch_count=%s", snapshot["mismatch_count"])
        return 0
    except Exception as exc:  # noqa: BLE001
        status = build_status(
            ok=False,
            out=args.out,
            status_out=args.status_out,
            error=str(exc),
            started_at=started_at,
        )
        if not args.no_write:
            _write_json_atomic(args.status_out, status)
        if args.json:
            print(json.dumps(status, indent=2, sort_keys=True))
        else:
            logger.error("supervisor/broker reconcile refresh failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
