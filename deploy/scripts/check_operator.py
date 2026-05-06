import json
import urllib.request

# Check operator queue for pending acks
r = urllib.request.urlopen("http://127.0.0.1:8000/api/jarvis/operator_queue", timeout=10)
d = json.loads(r.read())
items = d.get("items", []) if isinstance(d, list) else d.get("items", d.get("queue", []))
print(f"Operator queue items: {len(items) if isinstance(items, list) else 'N/A'}")

# Also check the bot strategy readiness
r2 = urllib.request.urlopen("http://127.0.0.1:8000/api/jarvis/bot_strategy_readiness", timeout=10)
d2 = json.loads(r2.read())
print(f"Strategy readiness entries: {len(d2)}")

# Check JARVIS summary for mode
r3 = urllib.request.urlopen("http://127.0.0.1:8000/api/jarvis/summary", timeout=10)
d3 = json.loads(r3.read())
print(json.dumps(d3, indent=2))
