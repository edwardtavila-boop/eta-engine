"""Tests for the operator-facing eta_status summary."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from eta_engine.scripts import eta_status as mod  # noqa: E402


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _patch_status_paths(monkeypatch, tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    monkeypatch.setattr(mod, "HEARTBEAT_PATH", state_dir / "heartbeat.json")
    monkeypatch.setattr(mod, "LEADERBOARD", state_dir / "leaderboard.json")
    monkeypatch.setattr(mod, "LAUNCH_READINESS", state_dir / "launch_readiness.json")
    monkeypatch.setattr(mod, "KAIZEN_LATEST", state_dir / "kaizen.json")
    monkeypatch.setattr(mod, "EVENTS_LOG", state_dir / "events.jsonl")
    monkeypatch.setattr(mod, "QUANTUM_DIR", state_dir / "quantum")
    monkeypatch.setattr(mod, "RETUNE_TRUTH_CHECK", state_dir / "health" / "diamond_retune_truth_check_latest.json")
    monkeypatch.setattr(mod, "PUBLIC_RETUNE_CACHE", state_dir / "health" / "public_diamond_retune_truth_latest.json")
    monkeypatch.setattr(
        mod,
        "PUBLIC_BROKER_CLOSE_CACHE",
        state_dir / "health" / "public_broker_close_truth_latest.json",
    )
    return state_dir


def test_gather_marks_stale_launch_readiness_and_keeps_gate_details(monkeypatch, tmp_path: Path) -> None:
    state_dir = _patch_status_paths(monkeypatch, tmp_path)
    stale_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()

    _write_json(state_dir / "heartbeat.json", {"ts": stale_ts, "tick_count": 9, "n_bots": 15, "mode": "paper_sim"})
    _write_json(state_dir / "leaderboard.json", {"n_diamonds": 14, "n_prop_ready": 0, "prop_ready_bots": []})
    _write_json(state_dir / "kaizen.json", {})
    _write_json(
        state_dir / "launch_readiness.json",
        {
            "ts": stale_ts,
            "overall_verdict": "NO_GO",
            "summary": "NO_GO - 1 hard gate failing",
            "launch_date": "2026-05-18",
            "days_until_launch": 4,
            "gates": [
                {
                    "name": "R1_PROP_READY_DESIGNATED",
                    "status": "NO_GO",
                    "rationale": "only 0 PROP_READY bot(s); need >= 2 before going live",
                    "detail": {"n": 0},
                },
                {
                    "name": "R7_LEDGER_FRESH",
                    "status": "HOLD",
                    "rationale": "ledger stale",
                    "detail": {"age_hours": 20.8},
                },
            ],
        },
    )

    state = mod.gather()
    launch = state["launch_readiness"]

    assert launch["stale"] is True
    assert launch["age_seconds"] >= 60 * 60
    assert launch["failing_gates"] == ["R1_PROP_READY_DESIGNATED"]
    assert launch["warning_gates"] == ["R7_LEDGER_FRESH"]
    assert launch["failing_gate_details"][0]["detail"]["n"] == 0
    assert "PROP_READY" in launch["failing_gate_details"][0]["rationale"]


def test_render_text_prints_readiness_freshness_and_gate_rationales() -> None:
    state = {
        "ts": "2026-05-14T00:00:00+00:00",
        "supervisor": {
            "tick_count": 1,
            "n_bots": 15,
            "mode": "paper_sim",
            "ts": "2026-05-14T00:00:00+00:00",
            "health": {
                "healthy": False,
                "status": "paper_main_loop_stuck",
                "diagnosis": "mock_main_heartbeat_stale_keepalive_fresh",
                "action_items": ["Restart ETAJarvisSupervisor via the ETA-Watchdog/SYSTEM task."],
            },
        },
        "fm_cache": {"hits": 0, "misses": 0, "hit_rate_pct": 0.0, "size": 0, "ttl_seconds": 0},
        "fm_breaker": {"spent_today_usd": 0.0, "cap_usd": 1.0, "headroom_pct": 100.0, "tripped": False},
        "diamond_leaderboard": {"n_diamonds": 14, "n_prop_ready": 0, "prop_ready_bots": []},
        "retune_advisory": {},
        "launch_readiness": {
            "verdict": "NO_GO",
            "days_until_launch": 4,
            "launch_date": "2026-05-18",
            "age_seconds": 7200.0,
            "stale": True,
            "ts": "2026-05-13T22:00:00+00:00",
            "failing_gates": ["R1_PROP_READY_DESIGNATED"],
            "warning_gates": ["R7_LEDGER_FRESH"],
            "failing_gate_details": [
                {
                    "name": "R1_PROP_READY_DESIGNATED",
                    "rationale": "only 0 PROP_READY bot(s); need >= 2 before going live",
                },
            ],
            "warning_gate_details": [{"name": "R7_LEDGER_FRESH", "rationale": "ledger stale"}],
            "summary": "NO_GO - 1 hard gate failing",
        },
        "kaizen": {"bootstraps": None, "n_bots": None, "applied_count": None, "ts": None, "action_counts": {}},
        "quantum_6h": {
            "rebalanced": None,
            "skipped": None,
            "total_cost_usd": 0.0,
            "ts": None,
            "instruments_with_signals": [],
            "instruments_with_hedges": [],
        },
        "recent_events": [],
    }

    text = mod.render_text(state)

    assert "freshness    : STALE" in text
    assert "health       : paper_main_loop_stuck" in text
    assert "Restart ETAJarvisSupervisor" in text
    assert "R1_PROP_READY_DESIGNATED: only 0 PROP_READY bot(s)" in text
    assert "R7_LEDGER_FRESH: ledger stale" in text


def test_gather_includes_public_advisory_retune_truth(monkeypatch, tmp_path: Path) -> None:
    state_dir = _patch_status_paths(monkeypatch, tmp_path)
    ts = datetime.now(UTC).isoformat()

    _write_json(state_dir / "heartbeat.json", {"ts": ts, "tick_count": 9, "n_bots": 15, "mode": "paper_sim"})
    _write_json(state_dir / "leaderboard.json", {"n_diamonds": 14, "n_prop_ready": 0, "prop_ready_bots": []})
    _write_json(state_dir / "kaizen.json", {})
    _write_json(state_dir / "launch_readiness.json", {"ts": ts, "overall_verdict": "NO_GO", "gates": []})
    _write_json(
        state_dir / "health" / "public_diamond_retune_truth_latest.json",
        {
            "kind": "eta_public_diamond_retune_truth_cache",
            "focus_bot": "mnq_futures_sage",
            "focus_issue": "broker_pnl_negative",
            "focus_state": "COLLECT_MORE_SAMPLE",
        },
    )
    _write_json(
        state_dir / "health" / "public_broker_close_truth_latest.json",
        {
            "kind": "eta_public_broker_close_truth_cache",
            "focus_bot": "mnq_futures_sage",
            "focus_closed_trade_count": 141,
            "focus_total_realized_pnl": -1939.75,
            "focus_profit_factor": 0.3951,
            "broker_mtd_pnl": 21338.0,
            "today_realized_pnl": -1751.99,
            "total_unrealized_pnl": 971.99,
            "open_position_count": 4,
            "reporting_timezone": "America/New_York",
        },
    )
    _write_json(
        state_dir / "health" / "diamond_retune_truth_check_latest.json",
        {
            "diagnosis": "public_local_focus_mismatch",
            "status": "warning",
            "warnings": ["Local canonical trade_closes source is thin."],
            "action_items": ["Refresh or repair the canonical trade_closes writer."],
        },
    )

    state = mod.gather()
    retune = state["retune_advisory"]

    assert retune["focus_bot"] == "mnq_futures_sage"
    assert retune["focus_closed_trade_count"] == 141
    assert retune["focus_total_realized_pnl"] == -1939.75
    assert retune["broker_mtd_pnl"] == 21338.0
    assert retune["diagnosis"] == "public_local_focus_mismatch"


def test_render_text_prints_public_advisory_retune_truth() -> None:
    state = {
        "ts": "2026-05-15T00:00:00+00:00",
        "supervisor": {
            "tick_count": 1,
            "n_bots": 15,
            "mode": "paper_sim",
            "ts": "2026-05-15T00:00:00+00:00",
            "health": {},
        },
        "fm_cache": {"hits": 0, "misses": 0, "hit_rate_pct": 0.0, "size": 0, "ttl_seconds": 0},
        "fm_breaker": {"spent_today_usd": 0.0, "cap_usd": 1.0, "headroom_pct": 100.0, "tripped": False},
        "diamond_leaderboard": {"n_diamonds": 14, "n_prop_ready": 0, "prop_ready_bots": []},
        "retune_advisory": {
            "focus_bot": "mnq_futures_sage",
            "focus_issue": "broker_pnl_negative",
            "focus_state": "COLLECT_MORE_SAMPLE",
            "focus_closed_trade_count": 141,
            "focus_total_realized_pnl": -1939.75,
            "focus_profit_factor": 0.3951,
            "broker_mtd_pnl": 21338.0,
            "today_realized_pnl": -1751.99,
            "total_unrealized_pnl": 971.99,
            "open_position_count": 4,
            "reporting_timezone": "America/New_York",
            "diagnosis": "public_local_focus_mismatch",
            "warnings": ["Local canonical trade_closes source is thin."],
            "action_items": ["Refresh or repair the canonical trade_closes writer."],
        },
        "launch_readiness": {
            "verdict": "NO_GO",
            "days_until_launch": 4,
            "launch_date": "2026-05-18",
            "age_seconds": 0.0,
            "stale": False,
            "ts": "2026-05-15T00:00:00+00:00",
            "failing_gates": [],
            "warning_gates": [],
            "failing_gate_details": [],
            "warning_gate_details": [],
            "summary": "",
        },
        "kaizen": {"bootstraps": None, "n_bots": None, "applied_count": None, "ts": None, "action_counts": {}},
        "quantum_6h": {
            "rebalanced": None,
            "skipped": None,
            "total_cost_usd": 0.0,
            "ts": None,
            "instruments_with_signals": [],
            "instruments_with_hedges": [],
        },
        "recent_events": [],
    }

    text = mod.render_text(state)

    assert "Retune truth    : mnq_futures_sage  COLLECT_MORE_SAMPLE  issue=broker_pnl_negative" in text
    assert "broker proof  : closes=141  pnl=$-1,939.75  pf=0.40" in text
    assert "broker state  : mtd=$+21,338.00  today=$-1,751.99  open=$+971.99  positions=4  tz=America/New_York" in text
    assert "local drift   : public_local_focus_mismatch" in text
    assert "action        : Refresh or repair the canonical trade_closes writer." in text


def test_render_text_includes_active_experiment_post_fix_sample() -> None:
    state = {
        "ts": "2026-05-15T00:00:00+00:00",
        "supervisor": {
            "status": "healthy",
            "summary": "fresh",
            "n_bots": 1,
            "tick_count": 123,
            "last_write_ts": "2026-05-15T00:00:00+00:00",
            "mode": "paper",
            "heartbeat_path": "C:/tmp/heartbeat.json",
        },
        "fm_cache": {"hits": 0, "misses": 0, "hit_rate_pct": 0.0, "size": 0, "ttl_seconds": 60},
        "fm_breaker": {"spent_today_usd": 0.0, "cap_usd": 10.0, "headroom_pct": 100.0, "tripped": False},
        "diamond_leaderboard": {"n_diamonds": 1, "n_prop_ready": 0, "prop_ready_bots": []},
        "retune_advisory": {
            "focus_bot": "mnq_futures_sage",
            "focus_issue": "broker_pnl_negative",
            "focus_state": "COLLECT_MORE_SAMPLE",
            "focus_closed_trade_count": 141,
            "focus_total_realized_pnl": -1939.75,
            "focus_profit_factor": 0.3951,
            "active_experiment": {
                "experiment_id": "partial_profit_disabled",
                "started_at": "2026-05-16T01:44:06+00:00",
                "partial_profit_enabled": False,
                "post_change_closed_trade_count": 2,
                "post_change_total_realized_pnl": 40.0,
                "post_change_profit_factor": 1.5,
            },
        },
        "launch_readiness": {
            "verdict": "NO_GO",
            "days_until_launch": 4,
            "launch_date": "2026-05-18",
            "age_seconds": 0.0,
            "stale": False,
            "ts": "2026-05-15T00:00:00+00:00",
            "failing_gates": [],
            "warning_gates": [],
            "failing_gate_details": [],
            "warning_gate_details": [],
            "summary": "",
        },
        "kaizen": {"bootstraps": None, "n_bots": None, "applied_count": None, "ts": None, "action_counts": {}},
        "quantum_6h": {
            "rebalanced": None,
            "skipped": None,
            "total_cost_usd": 0.0,
            "ts": None,
            "instruments_with_signals": [],
            "instruments_with_hedges": [],
        },
        "recent_events": [],
    }

    text = mod.render_text(state)

    assert "post-fix exp  : partial_profit_disabled since 2026-05-16T01:44:06+00:00" in text
    assert "partial_profit_enabled=False  closes=2  pnl=$+40.00  pf=1.50" in text
