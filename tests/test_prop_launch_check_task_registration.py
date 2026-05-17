from __future__ import annotations

import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from eta_engine.scripts import prop_launch_check as mod  # noqa: E402


def test_action_list_recommends_registration_script_for_missing_tasks() -> None:
    actions = mod._build_action_list(
        {
            "sections": [
                {
                    "name": "task_registration",
                    "status": "NO_GO",
                    "detail": {
                        "missing": [
                            "ETA-Diamond-LedgerEvery15Min",
                            "ETA-Diamond-PropAllocatorHourly",
                        ],
                    },
                }
            ]
        },
        {
            "counts": {
                "EVAL_LIVE": 0,
                "EVAL_PAPER": 10,
                "FUNDED_LIVE": 0,
                "RETIRED": 0,
            },
            "by_state": {},
        },
        {"n_prop_ready": 0},
        {"telegram": True, "discord": False, "generic": False},
        {"signal": "OK"},
        supervisor={"missing": False, "age_seconds": 1},
        candidates={"n_candidates": 1, "filter_candidates": [], "rejected_top5": []},
        live_capital_calendar={
            "live_capital_allowed_by_date": False,
            "not_before": "2026-07-08",
            "days_until_live_capital": 54,
        },
    )

    joined = "\n".join(actions)
    assert "register_diamond_cron_tasks.ps1" in joined
    assert "ETA-Diamond-LedgerEvery15Min" in joined
