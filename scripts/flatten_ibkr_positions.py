"""Flatten all open IBKR positions through the TWS/Gateway socket API.

This is the operator-facing "close everything now" helper for the IBKR paper
or live account currently connected on ``127.0.0.1:4002``. Unlike
``flatten_legacy_positions.py``, this script does not skip positions merely
because the supervisor still claims them. It is intended for emergency exits,
profit realization, and manual flatten events.
"""
# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any


def _ensure_main_thread_event_loop() -> None:
    """ib_insync/eventkit expects an event loop at import time on Python 3.14."""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


_ensure_main_thread_event_loop()

from ib_insync import IB, MarketOrder

from eta_engine.venues.ibkr_live import FUTURES_MAP

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FlattenResult:
    symbol: str
    local_symbol: str
    position: float
    action: str
    quantity: float
    status: str
    order_id: int | None = None
    detail: str = ""


def _action_for_position(position: float) -> str:
    if position > 0:
        return "SELL"
    if position < 0:
        return "BUY"
    raise ValueError("cannot derive flatten action for zero position")


def _patch_contract_exchange(contract: Any) -> None:  # noqa: ANN401
    if getattr(contract, "secType", "") != "FUT":
        return
    if getattr(contract, "exchange", ""):
        return
    mapping = FUTURES_MAP.get(str(getattr(contract, "symbol", "")).upper())
    if mapping:
        contract.exchange = mapping[1]


def _position_matches_filter(
    contract: Any,  # noqa: ANN401
    symbols: set[str] | None,
    locals_: set[str] | None,
) -> bool:
    """Decide whether a position should be acted on given the operator's
    --symbols / --local-symbols filters.

    Filters are matched case-insensitively against either the IBKR
    contract symbol (e.g. ``MNQ``) or the localSymbol (e.g. ``MNQM6``).
    When BOTH filters are None, every position matches — original
    "flatten everything" behaviour is preserved.

    When AT LEAST one filter is set, a position matches if it
    satisfies ANY of the provided filters (union, not intersection).
    """
    if not symbols and not locals_:
        return True
    sym = str(getattr(contract, "symbol", "") or "").upper()
    loc = str(getattr(contract, "localSymbol", "") or "").upper()
    # Normalize filter inputs too so callers using lowercase
    # work the same as the CLI which uppercases in main().
    sym_filter = {s.upper() for s in symbols} if symbols else None
    loc_filter = {s.upper() for s in locals_} if locals_ else None
    if sym_filter and sym in sym_filter:
        return True
    return bool(loc_filter and loc in loc_filter)


def flatten_ibkr_positions(
    *,
    host: str,
    port: int,
    client_id: int,
    confirm: bool,
    global_cancel: bool,
    symbols: set[str] | None = None,
    local_symbols: set[str] | None = None,
) -> list[FlattenResult]:
    """Flatten broker positions.  Selective via ``symbols`` (root
    contract, e.g. ``MNQ``) or ``local_symbols`` (front-month tag,
    e.g. ``MNQM6``).  Pass both as None to flatten everything."""
    ib = IB()
    results: list[FlattenResult] = []
    try:
        ib.connect(host, port, clientId=client_id, timeout=10)
        if global_cancel:
            with contextlib.suppress(Exception):
                ib.reqGlobalCancel()
                ib.sleep(0.25)

        for broker_position in ib.positions():
            qty = float(broker_position.position or 0.0)
            if abs(qty) < 1e-9:
                continue
            contract = broker_position.contract
            if not _position_matches_filter(contract, symbols, local_symbols):
                continue
            action = _action_for_position(qty)
            abs_qty = abs(qty)
            _patch_contract_exchange(contract)
            if not confirm:
                results.append(
                    FlattenResult(
                        symbol=str(contract.symbol),
                        local_symbol=str(getattr(contract, "localSymbol", "") or ""),
                        position=qty,
                        action=action,
                        quantity=abs_qty,
                        status="dry_run",
                        detail="pass --confirm to execute",
                    ),
                )
                continue

            order = MarketOrder(action, abs_qty)
            order.tif = "DAY"
            trade = ib.placeOrder(contract, order)
            for _ in range(50):
                ib.sleep(0.1)
                if trade.orderStatus.status in {"Submitted", "PreSubmitted", "Filled", "Cancelled", "Inactive"}:
                    break
            status = str(trade.orderStatus.status or "unknown")
            results.append(
                FlattenResult(
                    symbol=str(contract.symbol),
                    local_symbol=str(getattr(contract, "localSymbol", "") or ""),
                    position=qty,
                    action=action,
                    quantity=abs_qty,
                    status=status,
                    order_id=int(trade.order.orderId) if getattr(trade.order, "orderId", None) is not None else None,
                    detail=f"permId={getattr(trade.order, 'permId', 0)}",
                ),
            )
        return results
    finally:
        with contextlib.suppress(Exception):
            ib.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=901)
    parser.add_argument("--confirm", action="store_true", help="Actually submit the flatten market orders.")
    parser.add_argument(
        "--no-global-cancel",
        action="store_true",
        help="Skip reqGlobalCancel() before flattening positions.",
    )
    parser.add_argument(
        "--symbols",
        default="",
        help=(
            "Comma-separated root symbols to flatten (e.g. MNQ,MCL).  "
            "Case-insensitive.  Default empty = flatten ALL positions."
        ),
    )
    parser.add_argument(
        "--local-symbols",
        default="",
        help=(
            "Comma-separated localSymbols / front-month tags "
            "(e.g. MNQM6,MCLM6).  Case-insensitive.  Use this when "
            "you want to flatten one specific expiry but not others."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    sym_set: set[str] | None = None
    if ns.symbols:
        sym_set = {s.strip().upper() for s in ns.symbols.split(",") if s.strip()}
    local_set: set[str] | None = None
    if ns.local_symbols:
        local_set = {s.strip().upper() for s in ns.local_symbols.split(",") if s.strip()}

    try:
        results = flatten_ibkr_positions(
            host=ns.host,
            port=ns.port,
            client_id=ns.client_id,
            confirm=ns.confirm,
            global_cancel=not ns.no_global_cancel,
            symbols=sym_set,
            local_symbols=local_set,
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
                    "next_action": "Open IBKR Gateway/TWS paper API on this host and retry dry-run inspection.",
                },
                indent=2,
            ),
        )
        return 2
    print(json.dumps([asdict(row) for row in results], indent=2))
    if ns.confirm and any(row.status not in {"Submitted", "PreSubmitted", "Filled"} for row in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
