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
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


def _load_supervisor_open_position_symbols() -> set[str]:
    """Read the supervisor heartbeat to learn which symbols the
    supervisor currently claims a position on. Returns a set of
    symbol roots (MNQ, NQ, BTC, etc.)."""
    import json

    hb_path = ROOT / "state" / "jarvis_intel" / "supervisor" / "heartbeat.json"
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


async def _query_broker_positions() -> list[dict[str, Any]]:
    """Pull live broker positions via the supervisor's loop dispatcher.
    Returns a list of dicts with keys symbol/secType/position/avgCost."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        _get_live_ibkr_venue,
    )
    venue = _get_live_ibkr_venue()
    return await venue.get_positions()


async def _flatten_one(
    symbol: str, qty: float, *, dry_run: bool,
) -> tuple[bool, str]:
    """Submit a reduce-only market order to close ``qty`` of ``symbol``.

    Sign of qty: positive = long position to close (SELL), negative =
    short position to close (BUY). Returns (ok, reason)."""
    from eta_engine.venues.base import OrderRequest, OrderType, Side
    side = Side.SELL if qty > 0 else Side.BUY
    abs_qty = abs(qty)
    if dry_run:
        return True, f"dry-run {side.value} {abs_qty} {symbol}"

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        _get_live_ibkr_venue,
    )
    venue = _get_live_ibkr_venue()
    req = OrderRequest(
        symbol=symbol,
        side=side,
        qty=abs_qty,
        order_type=OrderType.MARKET,
        reduce_only=True,
        client_order_id=f"flatten_{symbol}_{int(abs_qty * 1e6)}",
    )
    result = await venue.place_order(req)
    return (
        result.status.value == "OPEN",
        f"{side.value} {abs_qty} {symbol} → {result.status.value} "
        f"({result.raw.get('reason') or result.raw.get('ibkr_order_id') or 'n/a'})",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm", action="store_true",
        help="Actually send the flatten orders. Without this, dry-run.",
    )
    parser.add_argument(
        "--symbol", default=None,
        help="Limit to a single symbol root (e.g. 'MNQ').",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    sup_claimed = _load_supervisor_open_position_symbols()
    logger.info("supervisor claims open positions on: %s", sorted(sup_claimed))

    try:
        broker_positions = asyncio.run(_query_broker_positions())
    except Exception as exc:  # noqa: BLE001
        logger.error("broker query failed: %s", exc)
        return 1

    targets: list[tuple[str, float]] = []
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
        targets.append((sym_raw, qty))

    if not targets:
        logger.info("no legacy positions to flatten")
        return 0

    logger.info(
        "would flatten %d position(s): %s%s",
        len(targets), targets,
        " (DRY RUN — pass --confirm to execute)" if not args.confirm else "",
    )

    if not args.confirm:
        return 0

    failures = 0
    for sym, qty in targets:
        try:
            ok, reason = asyncio.run(_flatten_one(sym, qty, dry_run=False))
        except Exception as exc:  # noqa: BLE001
            logger.error("flatten %s qty=%s failed: %s", sym, qty, exc)
            failures += 1
            continue
        logger.info("flatten %s: %s", sym, reason)
        if not ok:
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
