from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from eta_engine.venues.ibkr_live import LiveIbkrVenue, _filled_summary_from_statuses, _trade_submit_snapshot


def test_filled_summary_uses_parent_bracket_fill() -> None:
    statuses = [
        {"status": "Filled", "filled": 1.0, "avg_fill_price": 28708.0},
        {"status": "Submitted", "filled": 0.0, "avg_fill_price": 0.0},
        {"status": "PreSubmitted", "filled": 0.0, "avg_fill_price": 0.0},
    ]

    filled_qty, avg_price = _filled_summary_from_statuses(statuses)

    assert filled_qty == 1.0
    assert avg_price == 28708.0


def test_filled_summary_weights_multiple_fills() -> None:
    statuses = [
        {"status": "Filled", "filled": 1.0, "avg_fill_price": 100.0},
        {"status": "Filled", "filled": 2.0, "avg_fill_price": 103.0},
        {"status": "Submitted", "filled": 0.0, "avg_fill_price": 0.0},
    ]

    filled_qty, avg_price = _filled_summary_from_statuses(statuses)

    assert filled_qty == 3.0
    assert avg_price == 102.0


def test_trade_submit_snapshot_prefers_execution_fill_timestamp() -> None:
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=42, permId=0),
        orderStatus=SimpleNamespace(
            status="Filled",
            permId=31415,
            filled=1.0,
            remaining=0.0,
            avgFillPrice=28709.5,
        ),
        fills=[
            SimpleNamespace(
                execution=SimpleNamespace(time=datetime(2026, 5, 16, 14, 5, tzinfo=UTC)),
            ),
        ],
        log=[],
    )

    snapshot = _trade_submit_snapshot(trade)

    assert snapshot["filled_at"] == "2026-05-16T14:05:00+00:00"


def test_trade_submit_snapshot_falls_back_to_trade_log_fill_timestamp() -> None:
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=84, permId=0),
        orderStatus=SimpleNamespace(
            status="Filled",
            permId=27182,
            filled=1.0,
            remaining=0.0,
            avgFillPrice=28715.0,
        ),
        fills=[],
        log=[
            SimpleNamespace(
                status="Filled",
                time=datetime(2026, 5, 16, 14, 6, tzinfo=UTC),
            ),
        ],
    )

    snapshot = _trade_submit_snapshot(trade)

    assert snapshot["filled_at"] == "2026-05-16T14:06:00+00:00"


def test_live_ibkr_get_order_status_surfaces_canonical_filled_at() -> None:
    venue = LiveIbkrVenue()
    venue._orders["sig-filled"] = SimpleNamespace(
        orderStatus=SimpleNamespace(
            status="Filled",
            filled=1.0,
            avgFillPrice=28712.25,
        ),
        fills=[
            SimpleNamespace(
                execution=SimpleNamespace(time=datetime(2026, 5, 16, 14, 7, tzinfo=UTC)),
            ),
        ],
        log=[],
    )

    result = asyncio.run(venue.get_order_status("MNQ", "sig-filled"))

    assert result is not None
    assert result.status.value == "FILLED"
    assert result.filled_qty == 1.0
    assert result.avg_price == 28712.25
    assert result.filled_at == "2026-05-16T14:07:00+00:00"
