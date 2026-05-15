from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from eta_engine.scripts import supervisor_broker_reconcile_heartbeat as mod


def test_reconcile_snapshot_uses_current_broker_and_supervisor_positions() -> None:
    supervisor = {
        "path": r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\supervisor\heartbeat.json",
        "heartbeat_ts": "2026-05-15T02:56:00+00:00",
        "mode": "paper_live",
        "rows": [
            {
                "bot_id": "mnq_futures_sage",
                "symbol": "MNQ1",
                "root": "MNQ",
                "side": "BUY",
                "qty": 1.0,
                "signed_qty": 1.0,
            },
            {
                "bot_id": "mbt_funding_basis",
                "symbol": "MBT1",
                "root": "MBT",
                "side": "BUY",
                "qty": 1.0,
                "signed_qty": 1.0,
            },
        ],
    }
    broker_rows = [
        {"symbol": "MNQ", "root": "MNQ", "position": 3.0, "local_symbol": "MNQM6"},
        {"symbol": "MBT", "root": "MBT", "position": 1.0, "local_symbol": "MBTK6"},
        {"symbol": "MCL", "root": "MCL", "position": 1.0, "local_symbol": "MCLM6"},
        {"symbol": "MYM", "root": "MYM", "position": 1.0, "local_symbol": "MYMM6"},
    ]

    snapshot = mod.build_reconcile_snapshot(
        supervisor=supervisor,
        broker_rows=broker_rows,
        checked_at=datetime(2026, 5, 15, 2, 57, tzinfo=UTC),
        path=Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\supervisor\reconcile_last.json"),
    )

    assert snapshot["source"] == "supervisor_broker_reconcile_heartbeat"
    assert snapshot["broker_only"] == [
        {"symbol": "MCL", "broker_qty": 1.0},
        {"symbol": "MYM", "broker_qty": 1.0},
    ]
    assert snapshot["supervisor_only"] == []
    assert snapshot["divergent"] == [
        {
            "symbol": "MNQ",
            "broker_qty": 3.0,
            "supervisor_qty": 1.0,
            "delta": 2.0,
            "broker_excess_action": "SELL",
            "broker_excess_qty": 2.0,
        }
    ]
    assert snapshot["matched_positions"] == [{"symbol": "MBT", "broker_qty": 1.0, "supervisor_qty": 1.0}]
    assert snapshot["mismatch_count"] == 3
    assert snapshot["blocking_mismatch_count"] == 3
    assert snapshot["ready"] is False
    assert snapshot["order_action_allowed"] is False
    assert [row["category"] for row in snapshot["action_plan"]] == ["broker_only", "broker_only", "divergent"]


def test_supervisor_only_local_paper_position_is_visible_but_non_blocking() -> None:
    supervisor = {
        "path": r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\supervisor\heartbeat.json",
        "heartbeat_ts": "2026-05-15T05:09:43+00:00",
        "mode": "paper_live",
        "rows": [
            {
                "bot_id": "mbt_funding_basis",
                "symbol": "MBT1",
                "root": "MBT",
                "side": "BUY",
                "qty": 1.0,
                "signed_qty": 1.0,
            },
        ],
    }

    snapshot = mod.build_reconcile_snapshot(
        supervisor=supervisor,
        broker_rows=[],
        checked_at=datetime(2026, 5, 15, 5, 10, tzinfo=UTC),
        path=Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\supervisor\reconcile_last.json"),
    )

    assert snapshot["broker_only"] == []
    assert snapshot["supervisor_only"] == [{"symbol": "MBT", "supervisor_qty": 1.0}]
    assert snapshot["divergent"] == []
    assert snapshot["mismatch_count"] == 1
    assert snapshot["blocking_mismatch_count"] == 0
    assert snapshot["ready"] is True
    assert snapshot["action_plan"][0]["safe_default"] == "local_paper_only_no_broker_exposure"
    assert "not unknown broker exposure" in snapshot["action_plan"][0]["operator_review"]


def test_status_surface_never_allows_order_actions() -> None:
    status = mod.build_status(
        ok=True,
        out=Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\supervisor\reconcile_last.json"),
        status_out=Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\supervisor_broker_reconcile_heartbeat.json"),
        snapshot={
            "mismatch_count": 1,
            "broker_only": [{"symbol": "MCL"}],
            "supervisor_only": [],
            "divergent": [],
            "blocking_mismatch_count": 1,
            "ready": False,
        },
    )

    assert status["ok"] is True
    assert status["ready"] is False
    assert status["blocking_mismatch_count"] == 1
    assert status["order_action_allowed"] is False
    assert status["broker_only_symbols"] == ["MCL"]
    assert status["supervisor_only_symbols"] == []
    assert status["divergent_symbols"] == []


def test_symbol_root_normalizes_supervisor_and_crypto_symbols() -> None:
    assert mod._symbol_root("MNQ1") == "MNQ"
    assert mod._symbol_root("BTCUSD") == "BTC"
    assert mod._symbol_root("ETHUSDT") == "ETH"
