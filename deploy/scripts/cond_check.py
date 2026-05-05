"""Inspect recent CONDITIONAL JARVIS verdicts.

Reads from the canonical workspace path
``<workspace>/var/eta_engine/state/jarvis_intel/verdicts.jsonl`` per
CLAUDE.md hard rule #1; falls back to the legacy in-repo path during
the migration window so older logs remain inspectable.
"""

from __future__ import annotations

import json
from pathlib import Path

from eta_engine.scripts import workspace_roots


def _resolve_verdicts_path() -> Path:
    """Prefer canonical, fall back to legacy in-repo path for reads."""
    canonical = workspace_roots.ETA_JARVIS_VERDICTS_PATH
    if canonical.exists():
        return canonical
    return workspace_roots.ETA_LEGACY_JARVIS_VERDICTS_PATH


vp = _resolve_verdicts_path()
with open(vp, "rb") as f:
    lines = f.readlines()[-200:]

vs = [json.loads(l) for l in lines]
# Show only CONDITIONAL entries
conds = [v for v in vs if v.get("base_verdict") == "CONDITIONAL"]
print(f"CONDITIONAL: {len(conds)} out of {len(vs)}")
if conds:
    v = conds[-1]
    print("\nLast CONDITIONAL:")
    print(f"  subsystem: {v['subsystem']}")
    print(f"  reason: {v['base_reason']}")
    print(f"  size_mult: {v.get('final_size_multiplier', 'N/A')}")
    print(f"  cap_qty: {v.get('base_size_cap_qty', 'N/A')}")
    print(f"  confidence: {v.get('confidence', 'N/A')}")

    # Show all CONDITIONAL entries
    print("\nAll CONDITIONAL verdicts:")
    for v in conds[-10:]:
        print(f"  {v['ts'][:19]} {v['subsystem']:<18s} mult={v.get('final_size_multiplier','?'):>5} reason={v['base_reason']}")
