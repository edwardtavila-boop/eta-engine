from __future__ import annotations

from eta_engine.venues.ibkr_live import _filled_summary_from_statuses


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
