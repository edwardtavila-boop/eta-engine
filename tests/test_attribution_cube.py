"""Tests for attribution_cube — T12 multi-dim performance attribution."""
from __future__ import annotations

import json
from pathlib import Path


def _write_trades(path: Path, trades: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps(t) + "\n")


def test_query_empty_when_no_trade_closes(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import attribution_cube

    result = attribution_cube.query(
        slice_by=["bot"],
        trade_closes_path=tmp_path / "missing.jsonl",
    )
    assert result.rows == []
    assert result.error is None  # missing file is normal, not an error


def test_query_slices_by_bot(tmp_path: Path) -> None:
    """Group by bot_id → one row per bot with summed R."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(trades_path, [
        {"bot_id": "a", "asset_class": "MNQ", "r": 1.0, "ts": "2026-05-12T14:00:00Z"},
        {"bot_id": "a", "asset_class": "MNQ", "r": -0.5, "ts": "2026-05-12T15:00:00Z"},
        {"bot_id": "b", "asset_class": "BTC", "r": 2.0, "ts": "2026-05-12T16:00:00Z"},
    ])

    result = attribution_cube.query(
        slice_by=["bot"], trade_closes_path=trades_path,
    )
    by_key = {tuple(r.key.items()): r for r in result.rows}
    assert (("bot", "a"),) in by_key
    assert (("bot", "b"),) in by_key
    a = by_key[(("bot", "a"),)]
    b = by_key[(("bot", "b"),)]
    assert a.total_r == 0.5
    assert a.n_trades == 2
    assert a.win_rate == 0.5
    assert b.total_r == 2.0
    assert b.n_trades == 1


def test_query_slices_by_asset(tmp_path: Path) -> None:
    """Group by asset_class — different bots in same asset roll up together."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(trades_path, [
        {"bot_id": "a", "asset_class": "MNQ", "r": 1.0, "ts": "2026-05-12T14:00:00Z"},
        {"bot_id": "b", "asset_class": "MNQ", "r": 0.5, "ts": "2026-05-12T15:00:00Z"},
        {"bot_id": "c", "asset_class": "BTC", "r": -2.0, "ts": "2026-05-12T16:00:00Z"},
    ])

    result = attribution_cube.query(
        slice_by=["asset"], trade_closes_path=trades_path,
    )
    by_key = {r.key["asset"]: r for r in result.rows}
    assert by_key["MNQ"].total_r == 1.5
    assert by_key["BTC"].total_r == -2.0


def test_query_filters_by_asset(tmp_path: Path) -> None:
    """filter={'asset':'MNQ'} drops non-MNQ trades."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(trades_path, [
        {"bot_id": "a", "asset_class": "MNQ", "r": 1.0, "ts": "2026-05-12T14:00:00Z"},
        {"bot_id": "b", "asset_class": "BTC", "r": 100.0, "ts": "2026-05-12T15:00:00Z"},
    ])

    result = attribution_cube.query(
        slice_by=["bot"], filter={"asset": "MNQ"},
        trade_closes_path=trades_path,
    )
    bots = {r.key["bot"] for r in result.rows}
    assert bots == {"a"}


def test_query_filters_by_hour_window(tmp_path: Path) -> None:
    """hour_min/hour_max windows out trades outside the time range (UTC)."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(trades_path, [
        {"bot_id": "a", "asset_class": "MNQ", "r": 1.0, "ts": "2026-05-12T09:00:00Z"},
        {"bot_id": "a", "asset_class": "MNQ", "r": 2.0, "ts": "2026-05-12T14:00:00Z"},
        {"bot_id": "a", "asset_class": "MNQ", "r": 3.0, "ts": "2026-05-12T20:00:00Z"},
    ])

    result = attribution_cube.query(
        slice_by=["bot"],
        filter={"hour_min": 13, "hour_max": 18},
        trade_closes_path=trades_path,
    )
    # Only the 14:00 trade survives
    assert len(result.rows) == 1
    assert result.rows[0].total_r == 2.0
    assert result.rows[0].n_trades == 1


def test_query_school_expansion(tmp_path: Path) -> None:
    """Slicing by school expands each trade to N school rows."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(trades_path, [
        {
            "bot_id": "a",
            "asset_class": "MNQ",
            "r": 1.0,
            "ts": "2026-05-12T14:00:00Z",
            "schools": {"momentum": {"score": 0.5}, "mean_revert": {"score": -0.2}},
        },
    ])

    result = attribution_cube.query(
        slice_by=["school"], trade_closes_path=trades_path,
    )
    schools = {r.key["school"] for r in result.rows}
    assert schools == {"momentum", "mean_revert"}
    # Each school sees the full r=1.0 contribution (attribution overcounts —
    # that's by design for "who participated in winners")
    for r in result.rows:
        assert r.n_trades == 1
        assert r.total_r == 1.0


def test_query_multi_dim_slice(tmp_path: Path) -> None:
    """slice_by=['bot','asset'] produces composite-key rows."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(trades_path, [
        {"bot_id": "a", "asset_class": "MNQ", "r": 1.0, "ts": "2026-05-12T14:00:00Z"},
        {"bot_id": "a", "asset_class": "BTC", "r": 2.0, "ts": "2026-05-12T15:00:00Z"},
    ])

    result = attribution_cube.query(
        slice_by=["bot", "asset"], trade_closes_path=trades_path,
    )
    composite_keys = {(r.key["bot"], r.key["asset"]) for r in result.rows}
    assert composite_keys == {("a", "MNQ"), ("a", "BTC")}


def test_query_rejects_unknown_slice_dims(tmp_path: Path) -> None:
    """Unknown dim is silently dropped from slice_by."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(trades_path, [
        {"bot_id": "a", "asset_class": "MNQ", "r": 1.0, "ts": "2026-05-12T14:00:00Z"},
    ])

    result = attribution_cube.query(
        slice_by=["bot", "not_a_real_dim"],
        trade_closes_path=trades_path,
    )
    assert result.slice_by == ["bot"]


def test_query_rows_sorted_descending_by_total_r(tmp_path: Path) -> None:
    """Operator sees winners first."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(trades_path, [
        {"bot_id": "loser", "asset_class": "MNQ", "r": -5.0, "ts": "2026-05-12T14:00:00Z"},
        {"bot_id": "winner", "asset_class": "MNQ", "r": 10.0, "ts": "2026-05-12T15:00:00Z"},
        {"bot_id": "meh", "asset_class": "MNQ", "r": 0.0, "ts": "2026-05-12T16:00:00Z"},
    ])

    result = attribution_cube.query(
        slice_by=["bot"], trade_closes_path=trades_path,
    )
    bots_in_order = [r.key["bot"] for r in result.rows]
    assert bots_in_order == ["winner", "meh", "loser"]


def test_query_to_dict_serializable() -> None:
    """to_dict() returns pure-dict structure (for MCP envelope)."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    result = attribution_cube.query(slice_by=["bot"], trade_closes_path=Path("nope"))
    d = result.to_dict()
    assert isinstance(d, dict)
    assert "rows" in d
    assert "slice_by" in d
    assert "asof" in d
    # JSON round-trip
    json.dumps(d)
