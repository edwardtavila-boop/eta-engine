"""Test the exact execution path the supervisor uses."""

import asyncio
from pathlib import Path


async def main() -> None:
    print("=== Testing supervisor execution path ===")

    # 1. Test direct venue creation (same code as supervisor)
    print("\n1. Creating LiveIbkrVenue...")
    try:
        from eta_engine.venues.base import OrderRequest, OrderType, Side
        from eta_engine.venues.ibkr_live import LiveIbkrVenue

        venue = LiveIbkrVenue()
        report = await venue.connect()
        print(f"   Connected: {report.status} (details: {report.details})")

        # 2. Place a test order
        print("\n2. Placing test order (MNQ BUY qty=1)...")
        req = OrderRequest(
            symbol="MNQ",
            side=Side.BUY,
            qty=1,
            order_type=OrderType.MARKET,
        )
        result = await venue.place_order(req)
        print(f"   Result: {result.status.value}")
        print(f"   Raw: {result.raw}")

    except Exception as e:
        print(f"   ERROR: {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()

    # 3. Check the pending directory the supervisor writes to
    bf = Path("C:/EvolutionaryTradingAlgo/eta_engine/docs/btc_live/broker_fleet")
    print(f"\n3. Pending dir: {bf} (exists={bf.exists()})")

    # 4. Check if we can write to the pending dir
    try:
        test_file = bf / "test_perm_check.json"
        test_file.write_text('{"test": true}')
        test_file.unlink()
        print("   Writable: YES")
    except Exception as e:
        print(f"   Writable: NO ({e})")

    # 5. Check IBKR order count
    print("\n4. Checking IBKR orders via TWS...")
    try:
        from ib_insync import IB

        ib = IB()
        ib.connect("127.0.0.1", 4002, clientId=102, timeout=5)
        trades = ib.trades()
        print(f"   Trades: {len(trades)}")
        for t in trades[-3:]:
            print(f"   {t.contract.symbol} {t.order.action} x{t.order.totalQuantity} {t.orderStatus.status}")
        ib.disconnect()
    except Exception as e:
        print(f"   ERROR: {e}")


asyncio.run(main())
