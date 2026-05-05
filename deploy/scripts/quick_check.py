import json
import ssl
import urllib.request

ctx = ssl._create_unverified_context()

# Health
try:
    r = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5)
    print("Health:", json.loads(r.read()).get("status"))
except Exception as e:
    print("Health ERROR:", e)

# Fleet
try:
    r = urllib.request.urlopen("http://127.0.0.1:8000/api/bot-fleet", timeout=5)
    data = json.loads(r.read())
    bots = data.get("bots", [])
    app = sum(1 for b in bots if b.get("last_jarvis_verdict") == "APPROVED")
    cond = sum(1 for b in bots if b.get("last_jarvis_verdict") == "CONDITIONAL")
    total = len(bots)
    pnl = sum(b.get("todays_pnl", 0) for b in bots)
    print(f"Fleet: {total} bots, {app}APP/{cond}COND, PnL=${pnl:.0f}")
except Exception as e:
    print("Fleet ERROR:", e)

# IBKR auth
try:
    r = urllib.request.urlopen("https://127.0.0.1:5000/v1/api/iserver/auth/status", context=ctx, timeout=5)
    d = json.loads(r.read())
    print(f"IBKR: auth={d.get('authenticated')} conn={d.get('connected')}")
except Exception as e:
    print("IBKR ERROR:", e)

# IBKR orders
try:
    r = urllib.request.urlopen("https://127.0.0.1:5000/v1/api/iserver/account/orders", context=ctx, timeout=5)
    d = json.loads(r.read())
    orders = d.get("orders", [])
    print(f"Orders: {len(orders)} live")
except Exception as e:
    print("Orders ERROR:", e)

# Verdicts: canonical workspace path with legacy in-repo read fallback.
# Per CLAUDE.md hard rule #1, runtime state lives under
# <workspace>/var/eta_engine/state/. The legacy in-repo path remains
# readable during the migration window.
from pathlib import Path

from eta_engine.scripts import workspace_roots


def _resolve_verdicts_path() -> Path:
    canonical = workspace_roots.ETA_JARVIS_VERDICTS_PATH
    if canonical.exists():
        return canonical
    return workspace_roots.ETA_LEGACY_JARVIS_VERDICTS_PATH


vp = _resolve_verdicts_path()
if vp.exists():
    with open(vp, "rb") as f:
        lines = f.readlines()[-200:]
    vs = [json.loads(l) for l in lines]
    av = [v for v in vs if v.get("base_verdict") == "APPROVED"]
    cv = [v for v in vs if v.get("base_verdict") == "CONDITIONAL"]
    dv = [v for v in vs if v.get("base_verdict") == "DENIED"]
    confs = [v.get("confidence", 0) for v in av + cv]
    avg = sum(confs) / len(confs) if confs else 0
    print(f"Verdicts (last 200): {len(av)}APP/{len(cv)}COND/{len(dv)}DEN, avg conf={avg:.2f}")
    subs = {}
    for v in av + cv:
        s = v.get("subsystem", "?")
        subs[s] = subs.get(s, 0) + 1
    if subs:
        print("  By asset:", ", ".join(f"{s}:{c}" for s, c in sorted(subs.items(), key=lambda x: -x[1])))
else:
    print("Verdicts: file not found")
