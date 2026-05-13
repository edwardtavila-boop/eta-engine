"""IBKR Order Bridge — polls pending_order files and routes to LiveIbkrVenue.

Bridges the gap between JARVIS's ExecutionRouter (which writes
{bot_id}.pending_order.json when mode=paper_live) and the live
TWS API (port 4002).

Run as a background daemon:  python ibkr_order_bridge.py
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

# VPS paths
PENDING_DIR = Path("C:/EvolutionaryTradingAlgo/eta_engine/docs/btc_live/broker_fleet")
PROCESSED_DIR = PENDING_DIR / "processed"


async def process_order(venue: object, fpath: Path) -> None:
    """Read a pending order, submit through live venue, archive."""
    try:
        data = json.loads(fpath.read_text())
    except Exception:
        return

    symbol = data.get("symbol", "UNKNOWN")
    side = data.get("side", "BUY")
    qty = data.get("qty", 1)

    from eta_engine.venues.base import OrderRequest, OrderType, Side
    from eta_engine.venues.ibkr_live import LiveIbkrVenue

    venue = LiveIbkrVenue()
    await venue.connect()

    req = OrderRequest(
        symbol=symbol,
        side=Side.BUY if side.upper() == "BUY" else Side.SELL,
        qty=abs(float(qty)) or 1,
        order_type=OrderType.MARKET,
    )

    result = await venue.place_order(req)
    print(f"BRIDGE: {symbol} {side} x{qty} → {result.status} (id={result.order_id})")

    # Archive processed order
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    fpath.rename(PROCESSED_DIR / f"{fpath.stem}_{ts}.json")
    print(f"BRIDGE: Archived {fpath.name}")


async def main() -> None:
    print("IBKR Order Bridge starting...")
    print(f"Watching: {PENDING_DIR}")
    PENDING_DIR.mkdir(parents=True, exist_ok=True)

    from eta_engine.venues.ibkr_live import LiveIbkrVenue

    venue = LiveIbkrVenue()

    if not await venue._ensure_connected():
        print("BRIDGE: Cannot connect to TWS on port 4002 — retrying in 30s...")

    while True:
        pending = list(PENDING_DIR.glob("*.pending_order.json"))
        for fpath in pending:
            await process_order(venue, fpath)
        await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(main())
