import json
from pathlib import Path

d = Path("C:/EvolutionaryTradingAlgo/eta_engine/docs/btc_live/broker_fleet")
d.mkdir(parents=True, exist_ok=True)

# Test order
f = d / "test_direct.pending_order.json"
json.dump({
    "ts": "2026-05-04T13:30:00",
    "signal_id": "test_direct_001",
    "side": "BUY",
    "qty": 1,
    "symbol": "MNQ",
    "limit_price": 20000.0
}, open(f, "w"), indent=2)
print(f"Written: {f}")
print(f"Exists: {f.exists()}")
