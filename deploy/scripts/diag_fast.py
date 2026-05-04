import os, sys
from pathlib import Path

# Check supervisor execution
logs_dir = Path("C:/EvolutionaryTradingAlgo/eta_engine/var/logs")
if logs_dir.exists():
    logs = sorted(logs_dir.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
    print(f"Logs: {len(logs)}")
    for log in logs[:5]:
        print(f"  {log.name} ({log.stat().st_mtime})")
else:
    print("No log dir")

# Check broker fleet directory for pending orders
bf = Path("C:/EvolutionaryTradingAlgo/eta_engine/docs/btc_live/broker_fleet")
print(f"\nBroker fleet dir exists: {bf.exists()}")
pending = list(bf.glob("*.pending_order.json")) if bf.exists() else []
print(f"Pending orders: {len(pending)}")

# Check ibkr bridge log
bridge_log = Path("C:/EvolutionaryTradingAlgo/var/eta_engine/logs/ibkr_bridge.log")
print(f"\nBridge log exists: {bridge_log.exists()}")

# Check jarvis health
health = Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/jarvis_live_health.json")
if health.exists():
    import json
    d = json.loads(health.read_text())
    print(f"\nJARVIS health: {d.get('health', '?')}")
    print(f"  Reasons: {d.get('reasons', [])}")
