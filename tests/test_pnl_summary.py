"""Tests for pnl_summary — operator PnL aggregation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _write_trades(path: Path, trades: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps(t) + "\n")


def _ts(hours_ago: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


def test_summarize_empty_when_no_trade_closes(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    s = pnl_summary.summarize(
        window_hours=24,
        trade_closes_path=tmp_path / "missing.jsonl",
    )
    assert s.n_trades == 0
    assert s.total_r == 0.0
    assert s.best_trade is None


def test_summarize_aggregates_trades(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "a", "asset_class": "MNQ", "realized_r": 1.5, "ts": _ts(1)},
            {"bot_id": "a", "asset_class": "MNQ", "realized_r": -0.6, "ts": _ts(2)},
            {"bot_id": "b", "asset_class": "BTC", "realized_r": 2.0, "ts": _ts(3)},
        ],
    )
    s = pnl_summary.summarize(window_hours=24, trade_closes_path=path)
    assert s.n_trades == 3
    assert s.n_wins == 2
    assert s.n_losses == 1
    assert s.total_r == 2.9
    assert abs(s.win_rate - 2 / 3) < 0.001  # rounded to 4dp
    assert s.best_trade is not None
    assert s.best_trade.r == 2.0
    assert s.worst_trade is not None
    assert s.worst_trade.r == -0.6


def test_summarize_filters_window(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "recent", "realized_r": 1.0, "ts": _ts(1)},
            {"bot_id": "old", "realized_r": 5.0, "ts": _ts(100)},  # outside 24h
        ],
    )
    s = pnl_summary.summarize(window_hours=24, trade_closes_path=path)
    assert s.n_trades == 1
    assert s.total_r == 1.0


def test_summarize_returns_top_performers(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    path = tmp_path / "tc.jsonl"
    trades = []
    # Winner bot: +5R total
    for r in (1.0, 2.0, 2.0):
        trades.append({"bot_id": "winner", "realized_r": r, "ts": _ts(1)})
    # Mid bot: +1R total
    trades.append({"bot_id": "mid", "realized_r": 1.0, "ts": _ts(2)})
    # Loser: -3R total
    for r in (-1.0, -1.0, -1.0):
        trades.append({"bot_id": "loser", "realized_r": r, "ts": _ts(3)})
    _write_trades(path, trades)

    s = pnl_summary.summarize(window_hours=24, trade_closes_path=path)
    top_names = [b.bot_id for b in s.top_performers]
    assert top_names[0] == "winner"
    assert s.worst_performers[0].bot_id == "loser"


def test_summarize_handles_legacy_r_field(tmp_path: Path) -> None:
    """Records with `r` instead of `realized_r` still count (back-compat)."""
    from eta_engine.brain.jarvis_v3 import pnl_summary

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "legacy", "r": 1.5, "ts": _ts(1)},  # legacy field
        ],
    )
    s = pnl_summary.summarize(window_hours=24, trade_closes_path=path)
    assert s.n_trades == 1
    assert s.total_r == 1.5


def test_summarize_recent_returns_newest_first(tmp_path: Path) -> None:
    """``recent`` list is newest-first for the briefing template."""
    from eta_engine.brain.jarvis_v3 import pnl_summary

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "a", "realized_r": 1.0, "ts": _ts(10)},
            {"bot_id": "b", "realized_r": 2.0, "ts": _ts(5)},
            {"bot_id": "c", "realized_r": 3.0, "ts": _ts(1)},
        ],
    )
    s = pnl_summary.summarize(window_hours=24, trade_closes_path=path)
    assert s.recent[0].bot_id == "c"
    assert s.recent[1].bot_id == "b"
    assert s.recent[2].bot_id == "a"


def test_multi_window_summary_bundles_three_horizons(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "a", "realized_r": 1.0, "ts": _ts(1)},  # today + week + month
            {"bot_id": "b", "realized_r": 0.5, "ts": _ts(48)},  # week + month only
            {"bot_id": "c", "realized_r": 0.5, "ts": _ts(200)},  # month only
        ],
    )
    out = pnl_summary.multi_window_summary(trade_closes_path=path)
    assert out["today"]["n_trades"] == 1
    assert out["week"]["n_trades"] == 2
    assert out["month"]["n_trades"] == 3


def test_recent_trades_caps_at_n(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    path = tmp_path / "tc.jsonl"
    _write_trades(path, [{"bot_id": f"b{i}", "realized_r": 0.1 * i, "ts": _ts(i)} for i in range(10)])
    rt = pnl_summary.recent_trades(n=3, trade_closes_path=path)
    assert len(rt) == 3


def test_has_material_events_returns_false_when_quiet(tmp_path: Path) -> None:
    """No trades since asof + no R movement → not material."""
    from eta_engine.brain.jarvis_v3 import pnl_summary

    path = tmp_path / "tc.jsonl"
    _write_trades(path, [])
    asof = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    result = pnl_summary.has_material_events_since(
        asof_iso=asof,
        trade_closes_path=path,
    )
    assert result["has_material"] is False
    assert result["trades_since"] == 0


def test_has_material_events_fires_on_big_win(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "winner", "realized_r": 3.5, "ts": _ts(1)},
        ],
    )
    asof = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    result = pnl_summary.has_material_events_since(
        asof_iso=asof,
        trade_closes_path=path,
    )
    assert result["has_material"] is True
    assert any("big_win" in r for r in result["reasons"])


def test_has_material_events_fires_on_drawdown(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "a", "realized_r": -1.5, "ts": _ts(1)},
            {"bot_id": "b", "realized_r": -2.0, "ts": _ts(2)},
        ],
    )
    asof = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
    result = pnl_summary.has_material_events_since(
        asof_iso=asof,
        trade_closes_path=path,
    )
    assert result["has_material"] is True
    assert any("drawdown" in r for r in result["reasons"])


def test_has_material_events_fires_on_small_pnl_movement(tmp_path: Path) -> None:
    """Even 0.5R total counts as material — operator wants to see the move."""
    from eta_engine.brain.jarvis_v3 import pnl_summary

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "a", "realized_r": 0.6, "ts": _ts(1)},
        ],
    )
    asof = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    result = pnl_summary.has_material_events_since(
        asof_iso=asof,
        trade_closes_path=path,
    )
    assert result["has_material"] is True
    assert any("r_delta" in r for r in result["reasons"])


def test_summary_to_dict_is_json_serializable(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "a", "realized_r": 1.0, "ts": _ts(1)},
        ],
    )
    s = pnl_summary.summarize(window_hours=24, trade_closes_path=path)
    d = s.to_dict()
    json.dumps(d, default=str)  # must not raise
    assert d["n_trades"] == 1
    assert isinstance(d["recent"], list)


def test_summarize_handles_garbage_records(tmp_path: Path) -> None:
    """Non-JSON lines + non-numeric r are skipped silently."""
    from eta_engine.brain.jarvis_v3 import pnl_summary

    path = tmp_path / "tc.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write('{"bot_id":"x","realized_r":"not_a_number","ts":"' + _ts(1) + '"}\n')
        fh.write('{"bot_id":"good","realized_r":1.0,"ts":"' + _ts(1) + '"}\n')
    s = pnl_summary.summarize(window_hours=24, trade_closes_path=path)
    assert s.n_trades == 1
    assert s.total_r == 1.0
