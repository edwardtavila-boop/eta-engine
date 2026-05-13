"""Manual flatten — closes broker positions the supervisor doesn't claim.

Scenario this solves: a supervisor restart while broker positions
are still open. Reconcile detects them as ``broker_only`` (no
matching bot.open_position). The supervisor's normal exit logic
won't touch them — they sit at the broker accumulating risk until
their bracket children fire OR the operator flattens.

Use:
    python -m eta_engine.scripts.flatten_legacy_positions --confirm
        # Reads reconcile_last.json, queries broker for current
        # positions, submits reduce-only market orders to flatten
        # any position the supervisor doesn't claim.

    python -m eta_engine.scripts.flatten_legacy_positions  # dry-run
        # Lists what would be flattened without sending orders.

The script is idempotent: re-running after a successful flatten is
a no-op (broker has no positions, supervisor confirms zero state).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402


def _load_supervisor_open_position_symbols() -> set[str]:
    """Read the supervisor heartbeat to learn which symbols the
    supervisor currently claims a position on. Returns a set of
    symbol roots (MNQ, NQ, BTC, etc.)."""
    import json

    hb_path = workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH
    if not hb_path.exists():
        return set()
    try:
        payload = json.loads(hb_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    syms: set[str] = set()
    for bot in payload.get("bots", []):
        pos = bot.get("open_position")
        if pos:
            sym = str(bot.get("symbol", "")).upper()
            root = sym.rstrip("0123456789").replace("USD", "")
            if root:
                syms.add(root)
    return syms


def _query_broker_positions_sync() -> list[dict[str, Any]]:
    """Direct ib_insync call — fresh IB on its own clientId so we don't
    collide with the supervisor's venue loop.
    Returns a list of dicts with keys symbol/secType/position/avgCost."""
    from ib_insync import IB

    ib = IB()
    try:
        ib.connect("127.0.0.1", 4002, clientId=77, timeout=10)
        out = []
        for p in ib.positions():
            out.append(
                {
                    "symbol": p.contract.symbol,
                    "secType": p.contract.secType,
                    "position": float(p.position),
                    "avgCost": float(p.avgCost) if p.avgCost else 0.0,
                    "_contract": p.contract,
                }
            )
        return out
    finally:
        with contextlib.suppress(Exception):
            ib.disconnect()


def _flatten_one_sync(
    contract: object,
    qty: float,
    *,
    dry_run: bool,  # noqa: ANN401 — ib_insync Contract
) -> tuple[bool, str]:
    """Submit a reduce-only market order via fresh ib_insync.IB.

    Sign of qty: positive long → SELL to close; negative short → BUY.
    """
    from ib_insync import IB, MarketOrder

    action = "SELL" if qty > 0 else "BUY"
    abs_qty = abs(qty)
    sym = contract.symbol
    if dry_run:
        return True, f"dry-run {action} {abs_qty} {sym}"

    ib = IB()
    try:
        ib.connect("127.0.0.1", 4002, clientId=77, timeout=10)
        # ib.positions() returns Contracts with empty exchange — TWS
        # rejects placeOrder ('Missing order exchange'). Patch with the
        # canonical futures exchange before submit.
        if not getattr(contract, "exchange", "") and getattr(contract, "secType", "") == "FUT":
            from eta_engine.venues.ibkr_live import FUTURES_MAP

            mapping = FUTURES_MAP.get(contract.symbol)
            if mapping:
                contract.exchange = mapping[1]
        order = MarketOrder(action, abs_qty)
        order.tif = "GTC"
        trade = ib.placeOrder(contract, order)
        # Wait briefly for the broker to acknowledge
        for _ in range(30):  # up to ~3s
            ib.sleep(0.1)
            if trade.orderStatus.status in ("Submitted", "PreSubmitted", "Filled"):
                break
        status = trade.orderStatus.status
        return (
            status in ("Submitted", "PreSubmitted", "Filled"),
            f"{action} {abs_qty} {sym} → {status} (orderId={trade.order.orderId})",
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{action} {abs_qty} {sym} → exception: {exc}"
    finally:
        with contextlib.suppress(Exception):
            ib.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually send the flatten orders. Without this, dry-run.",
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="Limit to a single symbol root (e.g. 'MNQ').",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    sup_claimed = _load_supervisor_open_position_symbols()
    logger.info("supervisor claims open positions on: %s", sorted(sup_claimed))

    try:
        broker_positions = _query_broker_positions_sync()
    except Exception as exc:  # noqa: BLE001
        logger.error("broker query failed: %s", exc)
        return 1

    targets: list[tuple[Any, float]] = []
    for p in broker_positions or []:
        sym_raw = str(p.get("symbol", "")).upper()
        root = sym_raw.rstrip("0123456789").replace("USD", "")
        qty = float(p.get("position", 0) or 0)
        if abs(qty) < 1e-6:
            continue
        if root in sup_claimed:
            logger.info("skip %s qty=%s — supervisor claims this symbol", root, qty)
            continue
        if args.symbol and root != args.symbol.upper():
            continue
        targets.append((p["_contract"], qty))

    if not targets:
        logger.info("no legacy positions to flatten")
        return 0

    logger.info(
        "would flatten %d position(s): %s%s",
        len(targets),
        [(c.symbol, q) for c, q in targets],
        " (DRY RUN — pass --confirm to execute)" if not args.confirm else "",
    )

    if not args.confirm:
        return 0

    failures = 0
    for contract, qty in targets:
        try:
            ok, reason = _flatten_one_sync(contract, qty, dry_run=False)
        except Exception as exc:  # noqa: BLE001
            logger.error("flatten %s qty=%s failed: %s", contract.symbol, qty, exc)
            failures += 1
            continue
        logger.info("flatten %s: %s", contract.symbol, reason)
        if not ok:
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
