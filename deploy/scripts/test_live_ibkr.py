"""Smoke test: place a test order through LiveIbkrVenue."""
import asyncio

from eta_engine.venues.base import OrderRequest, OrderType, Side


async def main():
    from eta_engine.venues.ibkr_live import LiveIbkrVenue

    venue = LiveIbkrVenue()
    print("1. Connecting...")
    report = await venue.connect()
    print(f"   Status: {report.status}, Details: {report.details}")

    print("2. Getting balance...")
    balance = await venue.get_balance()
    print(f"   {balance}")

    print("3. Getting positions...")
    positions = await venue.get_positions()
    print(f"   {positions}")

    print("4. Placing test order (MNQ market buy, qty=1)...")
    req = OrderRequest(
        symbol="MNQ",
        side=Side.BUY,
        qty=1,
        order_type=OrderType.MARKET,
    )
    result = await venue.place_order(req)
    print(f"   OrderId: {result.order_id}, Status: {result.status}, Raw: {result.raw}")

if __name__ == "__main__":
    asyncio.run(main())
