"""Tests for broker-led diamond edge audit."""

from __future__ import annotations


def test_negative_broker_truth_creates_retune_action_with_session_gate() -> None:
    from eta_engine.scripts import diamond_edge_audit as audit

    closes = [
        {
            "bot_id": "mnq_futures_sage",
            "symbol": "MNQ",
            "session": "overnight",
            "realized_pnl": -200.0,
            "realized_r": -1.0,
            "ts": "2026-05-14T01:00:00+00:00",
        },
        {
            "bot_id": "mnq_futures_sage",
            "symbol": "MNQ",
            "session": "rth",
            "realized_pnl": 75.0,
            "realized_r": 0.5,
            "ts": "2026-05-14T14:35:00+00:00",
        },
        {
            "bot_id": "mnq_futures_sage",
            "symbol": "MNQ",
            "session": "rth",
            "realized_pnl": 25.0,
            "realized_r": 0.2,
            "ts": "2026-05-14T15:05:00+00:00",
        },
    ]
    assignments = {
        "mnq_futures_sage": {
            "symbol": "MNQ",
            "strategy_kind": "orb_sage_gated",
            "timeframe": "5m",
        },
    }

    report = audit.build_edge_audit(
        closes=closes,
        assignments=assignments,
        diamond_bots={"mnq_futures_sage"},
    )

    row = report["bots"][0]
    assert row["bot_id"] == "mnq_futures_sage"
    assert row["asset_sleeve"] == "equity_index"
    assert row["verdict"] == "RETUNE"
    assert row["total_realized_pnl"] == -100.0
    assert row["profit_factor"] == 0.5
    assert row["worst_session"]["session"] == "overnight"
    assert "block overnight" in row["recommended_action"]
    assert "opening range" in row["asset_playbook"].lower()
    assert report["retune_queue"][0]["bot_id"] == "mnq_futures_sage"
    assert report["summary"]["safe_to_mutate_live"] is False


def test_retune_queue_carries_broker_led_experiment_plan() -> None:
    from eta_engine.scripts import diamond_edge_audit as audit

    closes = [
        {
            "bot_id": "mcl_sweep_reclaim",
            "symbol": "MCL",
            "session": "afternoon",
            "realized_pnl": -120.0,
            "realized_r": -1.2,
        },
        {
            "bot_id": "mcl_sweep_reclaim",
            "symbol": "MCL",
            "session": "overnight",
            "realized_pnl": 45.0,
            "realized_r": 0.8,
        },
    ]
    assignments = {
        "mcl_sweep_reclaim": {
            "symbol": "MCL",
            "strategy_kind": "confluence_scorecard",
            "timeframe": "1h",
        },
    }

    report = audit.build_edge_audit(
        closes=closes,
        assignments=assignments,
        diamond_bots={"mcl_sweep_reclaim"},
    )

    queue_item = report["retune_queue"][0]
    assert queue_item["bot_id"] == "mcl_sweep_reclaim"
    assert queue_item["symbol"] == "MCL"
    assert queue_item["strategy_kind"] == "confluence_scorecard"
    assert queue_item["worst_session"] == "afternoon"
    assert queue_item["best_session"] == "overnight"
    assert "run_research_grid --source registry --bots mcl_sweep_reclaim" in queue_item["retune_command"]
    assert queue_item["live_mutation_policy"] == "paper_only_advisory"
    assert "event/session gate" in queue_item["parameter_focus"]
    assert "broker closes" in queue_item["paper_only_next_step"]
