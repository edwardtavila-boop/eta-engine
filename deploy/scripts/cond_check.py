import json
from pathlib import Path

vp = Path("C:/EvolutionaryTradingAlgo/eta_engine/state/jarvis_intel/verdicts.jsonl")
with open(vp, "rb") as f:
    lines = f.readlines()[-200:]

vs = [json.loads(l) for l in lines]
# Show only CONDITIONAL entries
conds = [v for v in vs if v.get("base_verdict") == "CONDITIONAL"]
print(f"CONDITIONAL: {len(conds)} out of {len(vs)}")
if conds:
    v = conds[-1]
    print(f"\nLast CONDITIONAL:")
    print(f"  subsystem: {v['subsystem']}")
    print(f"  reason: {v['base_reason']}")
    print(f"  size_mult: {v.get('final_size_multiplier', 'N/A')}")
    print(f"  cap_qty: {v.get('base_size_cap_qty', 'N/A')}")
    print(f"  confidence: {v.get('confidence', 'N/A')}")
    
    # Show all CONDITIONAL entries
    print(f"\nAll CONDITIONAL verdicts:")
    for v in conds[-10:]:
        print(f"  {v['ts'][:19]} {v['subsystem']:<18s} mult={v.get('final_size_multiplier','?'):>5} reason={v['base_reason']}")
