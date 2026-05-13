from __future__ import annotations

import json
from datetime import UTC, datetime

from eta_engine.scripts import l2_performance_attribution as attr


def _jsonl(path, records: list[dict]) -> None:  # noqa: ANN001
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")


def test_attribute_trade_decomposes_long_target_fill() -> None:
    result = attr.attribute_trade(
        signal={
            "signal_id": "sig-1",
            "side": "BUY",
            "entry_price": 100.0,
            "intended_target_price": 102.0,
            "intended_stop_price": 99.0,
        },
        entry_fill={"actual_fill_price": 99.75},
        exit_fill={"actual_fill_price": 102.25, "exit_reason": "TARGET"},
        point_value=2.0,
        commission_per_rt=1.0,
    )

    assert result.signal_id == "sig-1"
    assert result.pnl_total == 4.0
    assert result.entry_timing == 0.5
    assert result.exit_slip == 0.5
    assert result.commission == -1.0


def test_run_attribution_matches_signal_entry_and_exit(tmp_path) -> None:
    now = datetime.now(UTC).isoformat()
    signal_path = tmp_path / "signals.jsonl"
    fill_path = tmp_path / "fills.jsonl"
    _jsonl(
        signal_path,
        [
            {
                "ts": now,
                "strategy_id": "book_imbalance",
                "signal_id": "sig-1",
                "side": "BUY",
                "entry_price": 100.0,
                "intended_target_price": 102.0,
                "intended_stop_price": 99.0,
            },
        ],
    )
    _jsonl(
        fill_path,
        [
            {
                "ts": now,
                "signal_id": "sig-1",
                "exit_reason": "ENTRY",
                "actual_fill_price": 99.75,
            },
            {
                "ts": now,
                "signal_id": "sig-1",
                "exit_reason": "TARGET",
                "actual_fill_price": 102.25,
            },
        ],
    )

    report = attr.run_attribution(
        "book_imbalance",
        _signal_path=signal_path,
        _fill_path=fill_path,
        point_value=2.0,
        commission_per_rt=1.0,
    )

    assert report.n_trades == 1
    assert report.total_pnl == 4.0
    assert report.trades[0].signal_id == "sig-1"


def test_run_attribution_reports_empty_lifecycle(tmp_path) -> None:
    report = attr.run_attribution(
        "book_imbalance",
        _signal_path=tmp_path / "missing-signals.jsonl",
        _fill_path=tmp_path / "missing-fills.jsonl",
    )

    assert report.n_trades == 0
    assert report.notes
