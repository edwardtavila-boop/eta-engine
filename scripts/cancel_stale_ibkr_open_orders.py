"""Cancel only stale IBKR open orders identified by broker_bracket_audit.

This is intentionally narrower than ``flatten_ibkr_positions``:

* dry-run by default
* never calls ``reqGlobalCancel``
* only targets active orders whose symbol is listed in
  ``stale_flat_open_orders`` from the latest bracket audit, or in an explicit
  operator ``--symbols`` override
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eta_engine.scripts import broker_bracket_audit

if TYPE_CHECKING:
    from collections.abc import Callable

_DONE_STATUSES = {"apicancelled", "cancelled", "filled", "inactive"}


def _ensure_main_thread_event_loop() -> None:
    """ib_insync/eventkit expects an event loop at import time on Python 3.14."""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


@dataclass(slots=True)
class CancelCandidate:
    symbol: str
    local_symbol: str
    action: str
    order_type: str
    quantity: float
    order_id: int | None
    perm_id: int | None
    status: str


@dataclass(slots=True)
class CancelResult:
    symbol: str
    local_symbol: str
    action: str
    order_type: str
    quantity: float
    order_id: int | None
    perm_id: int | None
    status: str
    detail: str = ""


def _default_ib_factory() -> Any:  # noqa: ANN401
    _ensure_main_thread_event_loop()
    from ib_insync import IB  # noqa: PLC0415

    return IB()


def _clean_symbol(value: object) -> str:
    return broker_bracket_audit._clean_symbol(value)  # noqa: SLF001


def _symbol_root(value: object) -> str:
    return broker_bracket_audit._futures_root(value)  # noqa: SLF001


def _as_float(value: object) -> float:
    try:
        return abs(float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _as_int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _trade_contract(trade: object) -> object:
    return getattr(trade, "contract", None)


def _trade_order(trade: object) -> object:
    return getattr(trade, "order", None)


def _trade_status(trade: object) -> str:
    status = getattr(trade, "orderStatus", None)
    return str(getattr(status, "status", "") or "").strip()


def _trade_symbol(trade: object) -> str:
    contract = _trade_contract(trade)
    return _clean_symbol(getattr(contract, "symbol", "") if contract is not None else "")


def _trade_local_symbol(trade: object) -> str:
    contract = _trade_contract(trade)
    return _clean_symbol(getattr(contract, "localSymbol", "") if contract is not None else "")


def _trade_keys(trade: object) -> set[str]:
    keys: set[str] = set()
    for symbol in (_trade_symbol(trade), _trade_local_symbol(trade)):
        if not symbol:
            continue
        keys.add(symbol)
        root = _symbol_root(symbol)
        if root:
            keys.add(root)
    return keys


def _target_keys(*, stale_flat_open_orders: list[dict[str, Any]], symbols: set[str] | None = None) -> set[str]:
    raw_symbols = set(symbols or set())
    raw_symbols.update(
        str(order.get("symbol") or "")
        for order in stale_flat_open_orders
        if str(order.get("symbol") or "").strip()
    )
    keys: set[str] = set()
    for symbol in raw_symbols:
        cleaned = _clean_symbol(symbol)
        if not cleaned:
            continue
        keys.add(cleaned)
        root = _symbol_root(cleaned)
        if root:
            keys.add(root)
    return keys


def _candidate_from_trade(trade: object) -> CancelCandidate:
    order = _trade_order(trade)
    return CancelCandidate(
        symbol=_trade_symbol(trade),
        local_symbol=_trade_local_symbol(trade),
        action=str(getattr(order, "action", "") or "").strip().upper(),
        order_type=str(getattr(order, "orderType", "") or "").strip().upper(),
        quantity=_as_float(getattr(order, "totalQuantity", 0.0)),
        order_id=_as_int_or_none(getattr(order, "orderId", None)),
        perm_id=_as_int_or_none(getattr(order, "permId", None)),
        status=_trade_status(trade),
    )


def _result_from_candidate(candidate: CancelCandidate, *, status: str, detail: str) -> CancelResult:
    payload = asdict(candidate)
    payload["status"] = status
    payload["detail"] = detail
    return CancelResult(**payload)


def select_cancel_candidates(
    open_trades: list[object],
    *,
    stale_flat_open_orders: list[dict[str, Any]],
    symbols: set[str] | None = None,
) -> list[CancelCandidate]:
    targets = _target_keys(stale_flat_open_orders=stale_flat_open_orders, symbols=symbols)
    if not targets:
        return []
    candidates: list[CancelCandidate] = []
    seen: set[int | None] = set()
    for trade in open_trades:
        status = _trade_status(trade).lower()
        if status in _DONE_STATUSES:
            continue
        if not (_trade_keys(trade) & targets):
            continue
        candidate = _candidate_from_trade(trade)
        if candidate.quantity <= 0:
            continue
        if candidate.order_id in seen:
            continue
        seen.add(candidate.order_id)
        candidates.append(candidate)
    return candidates


def _load_audit(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _stale_flat_open_orders_from_audit(path: Path) -> list[dict[str, Any]]:
    payload = _load_audit(path)
    return [
        row
        for row in broker_bracket_audit._as_list(payload.get("stale_flat_open_orders"))  # noqa: SLF001
        if isinstance(row, dict)
    ]


def cancel_stale_ibkr_open_orders(
    *,
    host: str,
    port: int,
    client_id: int,
    confirm: bool,
    audit_path: Path = broker_bracket_audit.DEFAULT_OUT,
    symbols: set[str] | None = None,
    ib_factory: Callable[[], Any] = _default_ib_factory,
) -> list[CancelResult]:
    ib = ib_factory()
    stale_orders = _stale_flat_open_orders_from_audit(Path(audit_path))
    results: list[CancelResult] = []
    try:
        ib.connect(host, port, clientId=client_id, timeout=10)
        open_trades = list(ib.openTrades())
        target_keys = _target_keys(stale_flat_open_orders=stale_orders, symbols=symbols)
        candidates = select_cancel_candidates(
            open_trades,
            stale_flat_open_orders=stale_orders,
            symbols=symbols,
        )
        for trade in open_trades:
            candidate = _candidate_from_trade(trade)
            if candidate.order_id not in {row.order_id for row in candidates}:
                continue
            if not confirm:
                results.append(
                    _result_from_candidate(
                        candidate,
                        status="dry_run",
                        detail="pass --confirm to cancel this order",
                    ),
                )
                continue
            ib.cancelOrder(_trade_order(trade))
            with contextlib.suppress(Exception):
                ib.sleep(0.1)
            results.append(
                _result_from_candidate(
                    candidate,
                    status="cancel_submitted",
                    detail="cancelOrder submitted; no global cancel used",
                ),
            )
        if not results and target_keys:
            return []
        return results
    finally:
        with contextlib.suppress(Exception):
            ib.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=9031)
    parser.add_argument("--audit-path", type=Path, default=broker_bracket_audit.DEFAULT_OUT)
    parser.add_argument(
        "--symbols",
        default="",
        help="Optional comma-separated local/root symbols to cancel instead of relying only on the audit artifact.",
    )
    parser.add_argument("--confirm", action="store_true", help="Actually submit cancelOrder for selected orders.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    symbols = {symbol.strip().upper() for symbol in str(ns.symbols).split(",") if symbol.strip()} or None
    try:
        results = cancel_stale_ibkr_open_orders(
            host=ns.host,
            port=ns.port,
            client_id=ns.client_id,
            confirm=ns.confirm,
            audit_path=ns.audit_path,
            symbols=symbols,
        )
    except OSError as exc:
        print(
            json.dumps(
                {
                    "status": "connection_failed",
                    "host": ns.host,
                    "port": ns.port,
                    "client_id": ns.client_id,
                    "order_action_attempted": bool(ns.confirm),
                    "detail": str(exc),
                    "next_action": "Verify IBKR Gateway/TWS socket API on the VPS and retry dry-run inspection.",
                },
                indent=2,
            ),
        )
        return 2
    print(json.dumps([asdict(row) for row in results], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
