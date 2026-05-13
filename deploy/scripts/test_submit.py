"""Test the EXACT submit_entry path the supervisor uses."""

import asyncio
import json
from pathlib import Path

from eta_engine.scripts.jarvis_strategy_supervisor import BotInstance, ExecutionRouter, SupervisorConfig


async def main() -> None:
    print("=== Testing submit_entry execution path ===\n")

    # Setup
    cfg = SupervisorConfig()
    print(f"Mode: {cfg.mode}")
    print(f"Starting cash: {cfg.starting_cash_per_bot}")

    bf_dir = Path("C:/EvolutionaryTradingAlgo/eta_engine/docs/btc_live/broker_fleet")
    router = ExecutionRouter(cfg=cfg, bf_dir=bf_dir)

    # Create a test bot
    bot = BotInstance(bot_id="test_mnq", symbol="MNQ", cash=cfg.starting_cash_per_bot)

    # Simulate a bar
    bar = {"close": 19000.0, "open": 18950.0, "high": 19050.0, "low": 18900.0}

    # Test submit_entry directly (bypasses random gate)
    rec = router.submit_entry(
        bot=bot,
        signal_id="test_direct_002",
        side="BUY",
        bar=bar,
        size_mult=1.0,  # full size
    )

    print(f"\nResult: {rec is not None}")
    if rec:
        print(f"  Symbol: {rec.symbol}")
        print(f"  Side: {rec.side}")
        print(f"  Qty: {rec.qty}")
        print(f"  Fill price: {rec.fill_price}")

    # Check pending files
    print("\nPending files:")
    for f in sorted(bf_dir.glob("*.pending_order.json")):
        print(f"  {f.name}: {json.loads(f.read_text())}")


asyncio.run(main())
