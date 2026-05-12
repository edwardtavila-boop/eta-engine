"""Tests for kelly_optimizer — T13 fractional-Kelly sizing recommendations."""
from __future__ import annotations

import json
from pathlib import Path


def _write_trades(path: Path, trades: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps(t) + "\n")


def _recent_iso(days_ago: int = 0) -> str:
    from datetime import UTC, datetime, timedelta
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def test_recommend_empty_when_no_trade_closes(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    recs = kelly_optimizer.recommend_sizing(
        trade_closes_path=tmp_path / "missing.jsonl",
    )
    assert recs == []


def test_recommend_flags_insufficient_data(tmp_path: Path) -> None:
    """Bot with < MIN_OBS trades gets insufficient_data=True."""
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    path = tmp_path / "tc.jsonl"
    _write_trades(path, [
        {"bot_id": "low_data", "r": 1.0, "ts": _recent_iso(1)},
        {"bot_id": "low_data", "r": -0.5, "ts": _recent_iso(2)},
    ])
    recs = kelly_optimizer.recommend_sizing(trade_closes_path=path)
    assert len(recs) == 1
    assert recs[0]["bot_id"] == "low_data"
    assert recs[0]["insufficient_data"] is True
    # Insufficient-data bots default to 1.0× (no recommendation to change)
    assert recs[0]["recommended_size_modifier"] == 1.0


def test_recommend_with_sufficient_data(tmp_path: Path) -> None:
    """Bot with enough trades + positive expectancy gets a positive Kelly."""
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    path = tmp_path / "tc.jsonl"
    # 25 trades, mean +0.2R, decent edge
    trades = []
    for i in range(25):
        # Alternating wins of +1.0 and losses of -0.6 → mean +0.2, σ ≈ 0.8
        r = 1.0 if i % 2 == 0 else -0.6
        trades.append({"bot_id": "edge_bot", "r": r, "ts": _recent_iso(i)})
    _write_trades(path, trades)

    recs = kelly_optimizer.recommend_sizing(trade_closes_path=path)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["insufficient_data"] is False
    assert rec["n_trades"] == 25
    assert rec["avg_r"] > 0
    assert rec["f_kelly"] > 0
    # Final recommendation is clamped to [0, 1]
    assert 0.0 <= rec["recommended_size_modifier"] <= 1.0


def test_recommend_clamps_negative_expectancy_to_zero(tmp_path: Path) -> None:
    """Bot with consistent losses → recommended modifier 0.0 (no sizing)."""
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    path = tmp_path / "tc.jsonl"
    trades = [{"bot_id": "loser", "r": -0.5, "ts": _recent_iso(i)}
              for i in range(25)]
    _write_trades(path, trades)

    recs = kelly_optimizer.recommend_sizing(trade_closes_path=path)
    rec = next(r for r in recs if r["bot_id"] == "loser")
    # Negative mean → Kelly is negative; clamp brings recommendation to 0
    assert rec["f_kelly"] <= 0
    assert rec["recommended_size_modifier"] == 0.0


def test_recommend_lookback_window_filters_old_trades(tmp_path: Path) -> None:
    """Trades older than lookback_days are excluded."""
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    path = tmp_path / "tc.jsonl"
    trades = []
    # 20 trades 60 days ago (outside default 30-day lookback)
    for i in range(20):
        trades.append({"bot_id": "old_bot", "r": 0.5, "ts": _recent_iso(60 + i)})
    # 5 trades within 30 days
    for i in range(5):
        trades.append({"bot_id": "old_bot", "r": -0.1, "ts": _recent_iso(i)})
    _write_trades(path, trades)

    recs = kelly_optimizer.recommend_sizing(
        lookback_days=30, trade_closes_path=path,
    )
    # Only 5 trades within window → insufficient_data
    assert recs[0]["insufficient_data"] is True
    assert recs[0]["n_trades"] == 5


def test_recommend_drawdown_penalty_reduces_sizing(tmp_path: Path) -> None:
    """A bot with fat lower tail gets smaller f_adjusted than f_target."""
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    path = tmp_path / "tc.jsonl"
    # 25 trades: positive expectancy, but one massive loss.
    trades = []
    for i in range(24):
        trades.append({"bot_id": "fat_tail", "r": 0.6, "ts": _recent_iso(i)})
    trades.append({"bot_id": "fat_tail", "r": -10.0, "ts": _recent_iso(25)})  # disaster
    _write_trades(path, trades)

    recs = kelly_optimizer.recommend_sizing(trade_closes_path=path)
    rec = recs[0]
    # Penalty should make f_adjusted smaller than f_target
    assert rec["f_adjusted"] < rec["f_target"]


def test_recommend_handles_garbage_records(tmp_path: Path) -> None:
    """Malformed lines / missing bot_id / non-numeric r are skipped silently."""
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    path = tmp_path / "tc.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("not_json garbage\n")
        fh.write('{"bot_id":"a","r":"not_a_number","ts":"' + _recent_iso(1) + '"}\n')
        fh.write('{"r":0.5,"ts":"' + _recent_iso(2) + '"}\n')  # missing bot_id
        # 25 good records to pass MIN_OBS gate
        for i in range(25):
            fh.write('{"bot_id":"good","r":0.3,"ts":"' + _recent_iso(i) + '"}\n')

    recs = kelly_optimizer.recommend_sizing(trade_closes_path=path)
    bot_ids = {r["bot_id"] for r in recs}
    assert "good" in bot_ids
    # Bot with all-garbage records doesn't appear (no usable r values)
    good = next(r for r in recs if r["bot_id"] == "good")
    assert good["n_trades"] == 25


def test_recommend_recs_sorted_with_insufficient_data_last(tmp_path: Path) -> None:
    """Sufficient-data recs come first (sorted by recommended_size_modifier);
    insufficient-data recs at the end."""
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    path = tmp_path / "tc.jsonl"
    trades = []
    # Insufficient bot
    for i in range(5):
        trades.append({"bot_id": "tiny", "r": 0.1, "ts": _recent_iso(i)})
    # Sufficient bot
    for i in range(25):
        trades.append({"bot_id": "big", "r": 0.3, "ts": _recent_iso(i)})
    _write_trades(path, trades)

    recs = kelly_optimizer.recommend_sizing(trade_closes_path=path)
    # First entry must be the sufficient-data bot
    assert recs[0]["bot_id"] == "big"
    assert recs[0]["insufficient_data"] is False
    assert recs[-1]["bot_id"] == "tiny"
    assert recs[-1]["insufficient_data"] is True


def test_recommend_respects_kelly_fraction_arg(tmp_path: Path) -> None:
    """Larger kelly_fraction → larger f_target (linearly)."""
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    path = tmp_path / "tc.jsonl"
    trades = []
    for i in range(25):
        r = 0.5 if i % 2 == 0 else -0.3
        trades.append({"bot_id": "x", "r": r, "ts": _recent_iso(i)})
    _write_trades(path, trades)

    quarter = kelly_optimizer.recommend_sizing(
        kelly_fraction=0.25, trade_closes_path=path,
    )
    half = kelly_optimizer.recommend_sizing(
        kelly_fraction=0.5, trade_closes_path=path,
    )
    # f_target scales linearly with kelly_fraction
    assert abs(half[0]["f_target"] - 2 * quarter[0]["f_target"]) < 0.001


def test_recommend_handles_bad_args(tmp_path: Path) -> None:
    """Invalid lookback_days / kelly_fraction / drawdown_penalty fall back to defaults."""
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    path = tmp_path / "tc.jsonl"
    _write_trades(path, [
        {"bot_id": "x", "r": 0.2, "ts": _recent_iso(i)} for i in range(25)
    ])

    # Bad args → no crash, returns recommendations
    recs = kelly_optimizer.recommend_sizing(
        lookback_days=-5,  # negative → fallback to default
        kelly_fraction="not_a_number",  # type: ignore[arg-type]
        drawdown_penalty=None,  # type: ignore[arg-type]
        trade_closes_path=path,
    )
    assert len(recs) == 1
