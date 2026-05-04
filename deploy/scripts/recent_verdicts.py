import json
from pathlib import Path

vp = Path("C:/EvolutionaryTradingAlgo/eta_engine/state/jarvis_intel/verdicts.jsonl")
with open(vp, "rb") as f:
    lines = f.readlines()[-50:]

vs = [json.loads(l) for l in lines]
for v in vs:
    print(f"{v['ts'][:19]} {v['subsystem']:<18s} {v['base_verdict']:<12s} {v['base_reason']}")

print(f"\n--- {len(vs)} verdicts ---")
app = [v for v in vs if v.get("base_verdict") == "APPROVED"]
cond = [v for v in vs if v.get("base_verdict") == "CONDITIONAL"]
defer = [v for v in vs if v.get("base_verdict") == "DEFERRED"]
print(f"APP:{len(app)} COND:{len(cond)} DEFER:{len(defer)}")
