"""One-shot: record the two-project codebase-layout decision in roadmap_state.json."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"


def main() -> None:
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    state["shared_artifacts"]["codebase_layout_decision"] = {
        "locked_at_utc": datetime.now(UTC).isoformat(),
        "decision": "keep_separate",
        "projects": {
            "base_eta_engine": {
                "path": "Base/eta_engine/",
                "role": "multi-bot portfolio (6 bots + portfolio risk + funnel + staking + venues)",
                "layout": "obs/, funnel/, backtest/, core/, venues/, staking/",
                "tests": 604,
                "python_files": 107,
            },
            "the_firm_eta_engine": {
                "path": "OneDrive/The_Firm/eta_engine/",
                "role": "v3 framework (eta_v3_framework/ + firm/ + specs/ + Dockerfile + uv.lock)",
                "layout": "src/-based with separate test tree",
                "tests": "distinct suite (not counted here)",
            },
        },
        "interop": "firm_bridge.py shim — single import boundary",
        "migration_planned": False,
        "rationale": "architecturally distinct (portfolio vs framework); name collision only. Consolidation would break 604 portfolio tests and flatten two different design intents.",
        "operator_confirmed_by": "option_1_selection",
    }
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print("recorded codebase_layout_decision in roadmap_state.json")


if __name__ == "__main__":
    main()
