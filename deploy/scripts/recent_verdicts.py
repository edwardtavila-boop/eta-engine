"""Print the last 50 JARVIS verdicts.

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
    canonical = workspace_roots.ETA_JARVIS_VERDICTS_PATH
    if canonical.exists():
        return canonical
    return workspace_roots.ETA_LEGACY_JARVIS_VERDICTS_PATH


vp = _resolve_verdicts_path()
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
