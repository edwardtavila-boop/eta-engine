"""Tests for the canonical closed-trade ledger builder."""

from __future__ import annotations

import json
from pathlib import Path

from eta_engine.scripts import closed_trade_ledger as ledger


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_closed_trade_ledger_normalizes_summary_and_per_bot(tmp_path: Path) -> None:
    source = tmp_path / "trade_closes.jsonl"
    _write_jsonl(
        source,
        [
            {
                "ts": "2026-05-09T00:00:00+00:00",
                "signal_id": "s1",
                "bot_id": "volume_profile_mnq",
                "data_source": "paper",
                "realized_r": 1.5,
                "extra": {
                    "realized_pnl": 75,
                    "symbol": "MNQ1",
                    "side": "SELL",
                    "qty": 1,
                    "fill_price": 29350.25,
                },
            },
            {
                "ts": "2026-05-09T00:05:00+00:00",
                "signal_id": "s2",
                "bot_id": "volume_profile_mnq",
                "data_source": "paper",
                "realized_r": -1.0,
                "extra": {
                    "realized_pnl": -50,
                    "symbol": "MNQ1",
                    "side": "BUY",
                    "qty": 1,
                    "fill_price": 29325.25,
                },
            },
        ],
    )

    report = ledger.build_ledger_report(source_paths=[source])

    assert report["schema_version"] == 2
    assert report["data_sources_filter"] == ["live", "paper"]
    assert report["per_data_source_unfiltered"] == {"paper": 2}
    assert report["closed_trade_count"] == 2
    assert report["winning_trade_count"] == 1
    assert report["losing_trade_count"] == 1
    assert report["win_rate_pct"] == 50.0
    assert report["total_realized_pnl"] == 25.0
    assert report["cumulative_r"] == 0.5
    assert report["per_bot"]["volume_profile_mnq"]["closed_trade_count"] == 2
    assert report["recent_closes"][-1]["symbol"] == "MNQ1"
    assert report["recent_closes"][-1]["data_source"] == "paper"


def test_closed_trade_ledger_deduplicates_and_filters_bot(tmp_path: Path) -> None:
    source = tmp_path / "trade_closes.jsonl"
    row = {
        "ts": "2026-05-09T00:00:00+00:00",
        "signal_id": "s1",
        "bot_id": "volume_profile_mnq",
        "data_source": "paper",
        "realized_r": 1.0,
        "extra": {"realized_pnl": 10, "symbol": "MNQ1", "close_ts": "2026-05-09T00:00:01+00:00"},
    }
    _write_jsonl(
        source,
        [
            row,
            row,
            {
                "ts": "2026-05-09T00:01:00+00:00",
                "signal_id": "s2",
                "bot_id": "mym_sweep_reclaim",
                "data_source": "paper",
                "realized_r": -1.0,
                "extra": {"realized_pnl": -5, "symbol": "MYM1"},
            },
        ],
    )

    report = ledger.build_ledger_report(source_paths=[source], bot_filter="volume_profile_mnq")

    assert report["closed_trade_count"] == 1
    assert set(report["per_bot"]) == {"volume_profile_mnq"}


def test_closed_trade_ledger_excludes_unverified_rows_by_default(tmp_path: Path) -> None:
    source = tmp_path / "trade_closes.jsonl"
    _write_jsonl(
        source,
        [
            {
                "ts": "2026-05-09T00:00:00+00:00",
                "signal_id": "paper",
                "bot_id": "volume_profile_mnq",
                "data_source": "paper",
                "realized_r": 1.0,
                "extra": {"realized_pnl": 10, "symbol": "MNQ1"},
            },
            {
                "ts": "2026-05-09T00:01:00+00:00",
                "signal_id": "untagged",
                "bot_id": "volume_profile_mnq",
                "realized_r": 2.0,
                "extra": {"realized_pnl": 20, "symbol": "MNQ1"},
            },
        ],
    )

    report = ledger.build_ledger_report(source_paths=[source])

    assert report["closed_trade_count"] == 1
    assert report["total_realized_pnl"] == 10.0
    assert report["per_data_source_unfiltered"] == {"live_unverified": 1, "paper": 1}


def test_operator_filter_includes_canonical_untagged_rows(tmp_path: Path) -> None:
    source = tmp_path / "trade_closes.jsonl"
    _write_jsonl(
        source,
        [
            {
                "ts": "2026-05-09T00:00:00+00:00",
                "signal_id": "paper",
                "bot_id": "volume_profile_mnq",
                "data_source": "paper",
                "realized_r": 1.0,
                "extra": {"realized_pnl": 10, "symbol": "MNQ1"},
            },
            {
                "ts": "2026-05-09T00:01:00+00:00",
                "signal_id": "untagged",
                "bot_id": "volume_profile_mnq",
                "realized_r": 2.0,
                "extra": {"realized_pnl": 20, "symbol": "MNQ1"},
            },
        ],
    )

    report = ledger.build_ledger_report(
        source_paths=[source],
        data_sources=ledger.DEFAULT_OPERATOR_DATA_SOURCES,
    )

    assert report["closed_trade_count"] == 2
    assert report["total_realized_pnl"] == 30.0
    assert report["data_sources_filter"] == ["live", "live_unverified", "paper"]
