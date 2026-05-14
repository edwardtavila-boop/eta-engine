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
