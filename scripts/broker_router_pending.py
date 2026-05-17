from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_MAX_PENDING_ORDER_AGE_S = 15 * 60
_SMOKE_SIGNAL_TOKENS = ("smoke", "test", "dryrun", "dry_run")
_MIN_CRYPTO_LIMIT_PRICE: dict[str, float] = {
    "BTC": 1_000.0,
    "BTCUSD": 1_000.0,
    "BTCUSDT": 1_000.0,
    "ETH": 100.0,
    "ETHUSD": 100.0,
    "ETHUSDT": 100.0,
    "SOL": 1.0,
    "SOLUSD": 1.0,
    "SOLUSDT": 1.0,
    "XRP": 0.01,
    "XRPUSD": 0.01,
    "XRPUSDT": 0.01,
}

_FUTURES_ROOTS_TO_NORMALIZE = (
    "MNQ",
    "MES",
    "MGC",
    "MCL",
    "M6E",
    "MYM",
    "MBT",
    "NQ",
    "ES",
    "GC",
    "CL",
    "6E",
    "SI",
    "NG",
    "ZB",
    "ZN",
)


@dataclass(slots=True)
class PendingOrder:
    """One row of the supervisor pending-order JSON contract."""

    ts: str
    signal_id: str
    side: str
    qty: float
    symbol: str
    limit_price: float
    bot_id: str
    stop_price: float | None = None
    target_price: float | None = None
    reduce_only: bool = False
    execution_lane: str = ""
    capital_gate_scope: str = ""
    daily_loss_gate_mode: str = ""
    daily_loss_gate_active: bool = False
    daily_loss_gate_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_pending_file(path: Path) -> PendingOrder:
    """Parse one ``<bot_id>.pending_order.json`` file."""
    name = path.name
    if not name.endswith(".pending_order.json"):
        raise ValueError(f"unexpected filename pattern: {name!r}")
    bot_id = name[: -len(".pending_order.json")]
    if not bot_id:
        raise ValueError(f"empty bot_id in filename: {name!r}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"json read failed: {exc}") from exc

    required = ("ts", "signal_id", "side", "qty", "symbol", "limit_price")
    missing = [k for k in required if k not in payload]
    if missing:
        raise ValueError(f"missing fields {missing} in {name!r}")

    side = str(payload["side"]).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"invalid side {side!r}")

    try:
        qty = float(payload["qty"])
        limit_price = float(payload["limit_price"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"non-numeric qty/limit_price: {exc}") from exc

    if qty <= 0.0:
        raise ValueError(f"non-positive qty {qty}")

    stop_raw = payload.get("stop_price")
    target_raw = payload.get("target_price")
    try:
        stop_price = float(stop_raw) if stop_raw is not None else None
        target_price = float(target_raw) if target_raw is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError(f"non-numeric stop/target: {exc}") from exc

    reduce_only = bool(payload.get("reduce_only", False))

    return PendingOrder(
        ts=str(payload["ts"]),
        signal_id=str(payload["signal_id"]),
        side=side,
        qty=qty,
        symbol=_normalize_futures_symbol(str(payload["symbol"])),
        limit_price=limit_price,
        bot_id=bot_id,
        stop_price=stop_price,
        target_price=target_price,
        reduce_only=reduce_only,
        execution_lane=str(payload.get("execution_lane") or ""),
        capital_gate_scope=str(payload.get("capital_gate_scope") or ""),
        daily_loss_gate_mode=str(payload.get("daily_loss_gate_mode") or ""),
        daily_loss_gate_active=bool(payload.get("daily_loss_gate_active", False)),
        daily_loss_gate_reason=str(payload.get("daily_loss_gate_reason") or ""),
    )


def _normalize_futures_symbol(symbol: str) -> str:
    """Strip stray single-digit suffix from a known futures root."""
    if not symbol:
        return symbol
    for root in _FUTURES_ROOTS_TO_NORMALIZE:
        if symbol.startswith(root):
            rest = symbol[len(root) :]
            if len(rest) == 1 and rest.isdigit():
                return root
    return symbol


def pending_order_sanity_denial(order: PendingOrder) -> str:
    """Return a fail-closed reason for obviously unsafe live-routing intents."""
    signal_id = order.signal_id.strip().lower()
    if any(token in signal_id for token in _SMOKE_SIGNAL_TOKENS):
        return f"signal_id contains non-live token: {order.signal_id!r}"

    try:
        ts = datetime.fromisoformat(order.ts.replace("Z", "+00:00"))
    except ValueError:
        return f"invalid pending order ts: {order.ts!r}"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age_s = (datetime.now(UTC) - ts.astimezone(UTC)).total_seconds()
    if age_s > _MAX_PENDING_ORDER_AGE_S:
        return f"stale pending order age_s={age_s:.1f} max_s={_MAX_PENDING_ORDER_AGE_S}"

    if not order.reduce_only:
        if order.stop_price is None or order.target_price is None:
            return "missing bracket fields: stop_price and target_price are required"

        entry = float(order.limit_price)
        stop = float(order.stop_price)
        target = float(order.target_price)
        if entry <= 0.0 or stop <= 0.0 or target <= 0.0:
            return f"non-positive bracket geometry: entry={entry} stop={stop} target={target}"
        if order.side == "BUY" and not (stop < entry < target):
            return f"invalid BUY bracket geometry: stop={stop} entry={entry} target={target}"
        if order.side == "SELL" and not (target < entry < stop):
            return f"invalid SELL bracket geometry: target={target} entry={entry} stop={stop}"
    else:
        entry = float(order.limit_price)
        if entry <= 0.0:
            return f"non-positive exit limit_price: entry={entry}"

    symbol = order.symbol.strip().upper().lstrip("/")
    min_price = _MIN_CRYPTO_LIMIT_PRICE.get(symbol)
    if min_price is not None and entry < min_price:
        return f"implausible {symbol} limit_price={entry} below minimum sanity price={min_price}"

    return ""
