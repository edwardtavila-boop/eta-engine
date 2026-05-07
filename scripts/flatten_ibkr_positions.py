"""Flatten all open IBKR positions through the TWS/Gateway socket API.

This is the operator-facing "close everything now" helper for the IBKR paper
or live account currently connected on ``127.0.0.1:4002``. Unlike
``flatten_legacy_positions.py``, this script does not skip positions merely
because the supervisor still claims them. It is intended for emergency exits,
profit realization, and manual flatten events.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any

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


def _patch_contract_exchange(contract: Any) -> None:
    if getattr(contract, "secType", "") != "FUT":
        return
    if getattr(contract, "exchange", ""):
        return
    mapping = FUTURES_MAP.get(str(getattr(contract, "symbol", "")).upper())
    if mapping:
        contract.exchange = mapping[1]


def flatten_ibkr_positions(
    *,
    host: str,
    port: int,
    client_id: int,
    confirm: bool,
    global_cancel: bool,
) -> list[FlattenResult]:
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    results = flatten_ibkr_positions(
        host=ns.host,
        port=ns.port,
        client_id=ns.client_id,
        confirm=ns.confirm,
        global_cancel=not ns.no_global_cancel,
    )
    print(json.dumps([asdict(row) for row in results], indent=2))
    if ns.confirm and any(row.status not in {"Submitted", "PreSubmitted", "Filled"} for row in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
