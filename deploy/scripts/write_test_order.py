import json

from eta_engine.scripts import workspace_roots

d = workspace_roots.ETA_BROKER_ROUTER_PENDING_DIR
d.mkdir(parents=True, exist_ok=True)

# Test order
f = d / "test_direct.pending_order.json"
with f.open("w", encoding="utf-8") as handle:
    json.dump(
        {
            "ts": "2026-05-04T13:30:00",
            "signal_id": "test_direct_001",
            "side": "BUY",
            "qty": 1,
            "symbol": "MNQ",
            "limit_price": 20000.0,
        },
        handle,
        indent=2,
    )
print(f"Written: {f}")
print(f"Exists: {f.exists()}")
