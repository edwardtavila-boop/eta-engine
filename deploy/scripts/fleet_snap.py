import json
import urllib.request

r = urllib.request.urlopen("http://127.0.0.1:8000/api/bot-fleet", timeout=10)
d = json.loads(r.read())
bots = d.get("bots", [])
t = len(bots)
app = sum(1 for b in bots if b.get("last_jarvis_verdict", "") == "APPROVED")
cond = sum(1 for b in bots if b.get("last_jarvis_verdict", "") == "CONDITIONAL")
defer = sum(1 for b in bots if b.get("last_jarvis_verdict", "") == "DEFERRED")
deny = sum(1 for b in bots if b.get("last_jarvis_verdict", "") == "DENIED")
pnl = sum(b.get("todays_pnl", 0) for b in bots)
print(f"Bots: {t}  APP:{app} COND:{cond} DEFER:{defer} DENY:{deny}  PnL=${pnl:.0f}")
print(json.dumps(d.get("summary", {}), indent=2))
