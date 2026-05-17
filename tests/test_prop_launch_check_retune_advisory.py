from __future__ import annotations

import json
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from eta_engine.scripts import prop_launch_check as mod  # noqa: E402


def test_check_retune_advisory_reads_public_caches(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)
    health_dir = tmp_path / "var" / "eta_engine" / "state" / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    (health_dir / "public_diamond_retune_truth_latest.json").write_text(
        json.dumps(
            {
                "surface": {
                    "normalized": {
                        "focus_bot": "mnq_futures_sage",
                        "focus_issue": "broker_pnl_negative",
                        "focus_state": "COLLECT_MORE_SAMPLE",
                        "focus_closed_trade_count": 141,
                        "focus_total_realized_pnl": -1939.75,
                        "focus_profit_factor": 0.3951,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (health_dir / "public_broker_close_truth_latest.json").write_text(
        json.dumps(
            {
                "surface": {
                    "normalized": {
                        "focus_bot": "mnq_futures_sage",
                        "focus_closed_trade_count": 141,
                        "focus_total_realized_pnl": -1939.75,
                        "focus_profit_factor": 0.3951,
                        "broker_mtd_pnl": 20087.0,
                        "today_realized_pnl": -1752.06,
                        "total_unrealized_pnl": -278.94,
                        "open_position_count": 4,
                        "broker_snapshot_source": "ibkr_probe_cache",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (health_dir / "diamond_retune_truth_check_latest.json").write_text(
        json.dumps(
            {
                "diagnosis": "public_local_focus_mismatch",
                "warnings": ["Public retune focus and local canonical retune receipt disagree."],
                "action_items": ["Refresh the canonical trade_closes writer before trusting local-only counts."],
            }
        ),
        encoding="utf-8",
    )

    advisory = mod._check_retune_advisory()

    assert advisory["available"] is True
    assert advisory["focus_bot"] == "mnq_futures_sage"
    assert advisory["focus_closed_trade_count"] == 141
    assert advisory["focus_total_realized_pnl"] == -1939.75
    assert advisory["broker_mtd_pnl"] == 20087.0
    assert advisory["diagnosis"] == "public_local_focus_mismatch"
    assert advisory["preferred_action"] == (
        "Refresh the canonical trade_closes writer before trusting local-only counts."
    )


def test_build_action_list_adds_retune_advisory_guidance() -> None:
    actions = mod._build_action_list(
        {"sections": []},
        {
            "counts": {
                "EVAL_LIVE": 0,
                "EVAL_PAPER": 10,
                "FUNDED_LIVE": 0,
                "RETIRED": 0,
            },
            "by_state": {},
        },
        {"n_prop_ready": 2},
        {"telegram": True, "discord": False, "generic": False},
        {"signal": "OK"},
        supervisor={"missing": False, "age_seconds": 1},
        candidates={"n_candidates": 1, "filter_candidates": [], "rejected_top5": []},
        live_capital_calendar={
            "live_capital_allowed_by_date": True,
            "not_before": "2026-07-08",
            "days_until_live_capital": 0,
        },
        retune_advisory={
            "available": True,
            "focus_bot": "mnq_futures_sage",
            "focus_state": "COLLECT_MORE_SAMPLE",
            "focus_issue": "broker_pnl_negative",
            "focus_closed_trade_count": 141,
            "focus_total_realized_pnl": -1939.75,
            "focus_profit_factor": 0.3951,
            "diagnosis": "public_local_focus_mismatch",
            "preferred_action": "Refresh the canonical trade_closes writer before trusting local-only counts.",
        },
    )

    joined = "\n".join(actions)
    assert "Broker-backed retune truth still flags mnq_futures_sage" in joined
    assert "Do not treat it as a launch candidate yet." in joined
    assert "Refresh the canonical trade_closes writer before trusting local-only counts." in joined


def test_build_action_list_surfaces_active_post_fix_experiment() -> None:
    actions = mod._build_action_list(
        {"sections": []},
        {
            "counts": {
                "EVAL_LIVE": 0,
                "EVAL_PAPER": 10,
                "FUNDED_LIVE": 0,
                "RETIRED": 0,
            },
            "by_state": {},
        },
        {"n_prop_ready": 2},
        {"telegram": True, "discord": False, "generic": False},
        {"signal": "OK"},
        supervisor={"missing": False, "age_seconds": 1},
        candidates={"n_candidates": 1, "filter_candidates": [], "rejected_top5": []},
        live_capital_calendar={
            "live_capital_allowed_by_date": True,
            "not_before": "2026-07-08",
            "days_until_live_capital": 0,
        },
        retune_advisory={
            "available": True,
            "focus_bot": "mnq_futures_sage",
            "focus_state": "COLLECT_MORE_SAMPLE",
            "focus_issue": "broker_pnl_negative",
            "focus_closed_trade_count": 141,
            "focus_total_realized_pnl": -1939.75,
            "focus_profit_factor": 0.3951,
            "diagnosis": "public_local_focus_match",
            "active_experiment": {
                "experiment_id": "partial_profit_disabled",
                "started_at": "2026-05-16T01:44:06+00:00",
                "partial_profit_enabled": False,
                "post_change_closed_trade_count": 2,
                "post_change_total_realized_pnl": 40.0,
                "post_change_profit_factor": 1.5,
            },
        },
    )

    joined = "\n".join(actions)
    assert "Corrected mnq_futures_sage experiment active since 2026-05-16T01:44:06+00:00" in joined
    assert "post-fix sample n=2" in joined
    assert "partial_profit_enabled=false" in joined


def test_print_human_renders_retune_advisory_section(capsys) -> None:
    report = {
        "ts": "2026-05-15T20:10:00+00:00",
        "dryrun": {"overall_verdict": "HOLD", "summary": "paper-only window", "sections": []},
        "lifecycle": {
            "counts": {"EVAL_LIVE": 0, "EVAL_PAPER": 9, "FUNDED_LIVE": 0, "RETIRED": 0},
            "by_state": {},
        },
        "leaderboard": {"n_prop_ready": 0, "missing": True},
        "alert_channels": {"telegram": True, "discord": False, "generic": False},
        "drawdown_guard": {"missing": True},
        "supervisor": {
            "missing": False,
            "age_seconds": 2,
            "tick_count": 1,
            "mode": "paper_live",
            "feed": "ibkr",
            "n_bots": 9,
            "live_money_enabled": False,
        },
        "live_capital_calendar": {
            "live_capital_allowed_by_date": False,
            "today": "2026-05-15",
            "not_before": "2026-07-08",
            "days_until_live_capital": 54,
            "reason": "paper-only window",
        },
        "launch_candidates": {"n_candidates": 0, "n_filter_candidates": 0, "rejected_top5": []},
        "retune_advisory": {
            "available": True,
            "focus_bot": "mnq_futures_sage",
            "focus_state": "COLLECT_MORE_SAMPLE",
            "focus_issue": "broker_pnl_negative",
            "focus_closed_trade_count": 141,
            "focus_total_realized_pnl": -1939.75,
            "focus_profit_factor": 0.3951,
            "broker_mtd_pnl": 20087.0,
            "today_realized_pnl": -1752.06,
            "total_unrealized_pnl": -278.94,
            "open_position_count": 4,
            "broker_snapshot_source": "ibkr_probe_cache",
            "diagnosis": "public_local_focus_mismatch",
            "preferred_warning": "Public retune focus and local canonical retune receipt disagree.",
        },
        "actions": [],
    }

    mod._print_human(report)
    out = capsys.readouterr().out

    assert "Broker-backed retune advisory" in out
    assert "focus=mnq_futures_sage state=COLLECT_MORE_SAMPLE issue=broker_pnl_negative" in out
    assert "local drift: public_local_focus_mismatch" in out
