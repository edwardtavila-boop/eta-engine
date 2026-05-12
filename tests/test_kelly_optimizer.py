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


def test_recommend_reads_realized_r_canonical_field(tmp_path: Path) -> None:
    """Regression: kelly_optimizer must read ``realized_r`` (the canonical
    field name in ``jarvis_intel/trade_closes.jsonl``) — not just the legacy
    ``r`` / ``r_value`` aliases.

    Pre-fix behavior: every bot reported avg_r=0.000 because the optimizer
    looked for ``r`` / ``r_value`` but the writer emits ``realized_r``. This
    silently corrupted every Kelly recommendation for months until a Zeus
    snapshot surfaced the avg_r=0 anomaly.
    """
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    path = tmp_path / "tc.jsonl"
    # 25 trades using ONLY the realized_r field (no r, no r_value)
    trades = [
        {"bot_id": "real_bot", "realized_r": 1.0, "ts": _recent_iso(i)}
        if i % 2 == 0
        else {"bot_id": "real_bot", "realized_r": -0.5, "ts": _recent_iso(i)}
        for i in range(25)
    ]
    _write_trades(path, trades)

    recs = kelly_optimizer.recommend_sizing(trade_closes_path=path)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["bot_id"] == "real_bot"
    assert rec["insufficient_data"] is False
    # Mean of alternating +1.0 / -0.5 over 25 trades = ~+0.26 (13 wins, 12 losses)
    assert rec["avg_r"] > 0.2, f"avg_r={rec['avg_r']} should reflect realized_r values, not default 0"


def test_recommend_legacy_r_alias_still_works(tmp_path: Path) -> None:
    """Back-compat: bots writing the legacy ``r`` field continue to work."""
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    path = tmp_path / "tc.jsonl"
    trades = [
        {"bot_id": "legacy_bot", "r": 0.3, "ts": _recent_iso(i)}
        for i in range(25)
    ]
    _write_trades(path, trades)

    recs = kelly_optimizer.recommend_sizing(trade_closes_path=path)
    assert len(recs) == 1
    assert recs[0]["avg_r"] == 0.3
    assert recs[0]["insufficient_data"] is False


def test_recommend_realized_r_overrides_legacy_when_both_present(
    tmp_path: Path,
) -> None:
    """When both ``realized_r`` and ``r`` are present, the canonical
    ``realized_r`` wins (writer-side mismatch shouldn't silently use
    the wrong number)."""
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    path = tmp_path / "tc.jsonl"
    trades = [
        {"bot_id": "dual", "realized_r": 0.5, "r": -99.0, "ts": _recent_iso(i)}
        for i in range(25)
    ]
    _write_trades(path, trades)

    recs = kelly_optimizer.recommend_sizing(trade_closes_path=path)
    assert recs[0]["avg_r"] == 0.5  # realized_r, not r=-99


def test_read_trade_closes_unions_canonical_and_legacy(
    tmp_path: Path, monkeypatch: object,
) -> None:
    """Regression: when called with no override path, the reader must
    consult BOTH ``DEFAULT_TRADE_CLOSES_PATH`` (canonical) and
    ``_LEGACY_TRADE_CLOSES_PATH`` (the eta_engine/state archive that
    holds 99%+ of historical trades on production installs).

    Pre-fix behavior: kelly_optimizer read only the canonical path and
    reported ``insufficient_data`` for every diamond bot, because all
    their history lived in the legacy archive. After fix: both paths
    are read and deduped on (signal_id, bot_id, ts, realized_r).
    """
    from eta_engine.brain.jarvis_v3 import kelly_optimizer as ko

    canonical = tmp_path / "canonical.jsonl"
    legacy = tmp_path / "legacy.jsonl"
    # 10 canonical rows + 15 legacy rows = 25 total, exceeds MIN_OBS
    _write_trades(canonical, [
        {"bot_id": "merged_bot", "signal_id": f"can_{i}",
         "realized_r": 0.4, "ts": _recent_iso(i)}
        for i in range(10)
    ])
    _write_trades(legacy, [
        {"bot_id": "merged_bot", "signal_id": f"leg_{i}",
         "realized_r": -0.2, "ts": _recent_iso(i + 20)}
        for i in range(15)
    ])
    monkeypatch.setattr(ko, "DEFAULT_TRADE_CLOSES_PATH", canonical)  # type: ignore[attr-defined]
    monkeypatch.setattr(ko, "_LEGACY_TRADE_CLOSES_PATH", legacy)  # type: ignore[attr-defined]

    # Call with NO override path → must read both
    recs = ko.recommend_sizing(lookback_days=365)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["bot_id"] == "merged_bot"
    assert rec["n_trades"] == 25  # 10 + 15 deduped (no signal-id overlap)
    assert rec["insufficient_data"] is False
    # Mean of (10 × 0.4 + 15 × -0.2) / 25 = (4 - 3) / 25 = 0.04
    assert abs(rec["avg_r"] - 0.04) < 0.001


def test_read_trade_closes_dedupes_dual_source(
    tmp_path: Path, monkeypatch: object,
) -> None:
    """Records present in BOTH sources (same signal_id, bot_id, ts,
    realized_r) must be deduped — counted once, not twice."""
    from eta_engine.brain.jarvis_v3 import kelly_optimizer as ko

    canonical = tmp_path / "canonical.jsonl"
    legacy = tmp_path / "legacy.jsonl"
    # 25 trades, identical content in both files
    shared = [
        {"bot_id": "dedup_bot", "signal_id": f"shared_{i}",
         "realized_r": 0.3, "ts": _recent_iso(i)}
        for i in range(25)
    ]
    _write_trades(canonical, shared)
    _write_trades(legacy, shared)
    monkeypatch.setattr(ko, "DEFAULT_TRADE_CLOSES_PATH", canonical)  # type: ignore[attr-defined]
    monkeypatch.setattr(ko, "_LEGACY_TRADE_CLOSES_PATH", legacy)  # type: ignore[attr-defined]

    recs = ko.recommend_sizing(lookback_days=365)
    assert len(recs) == 1
    # Dedupe = 25 not 50
    assert recs[0]["n_trades"] == 25, (
        "duplicate rows across canonical + legacy must dedupe to one count, "
        f"got {recs[0]['n_trades']}"
    )


def test_read_trade_closes_override_is_single_source(tmp_path: Path) -> None:
    """When the caller supplies an explicit ``trade_closes_path=``, the
    reader behaves as a single-source reader (tests use this to feed
    curated tmp_path data without accidentally reading the production
    canonical/legacy archives)."""
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    override = tmp_path / "only_this.jsonl"
    _write_trades(override, [
        {"bot_id": "iso", "realized_r": 0.5, "ts": _recent_iso(i)}
        for i in range(25)
    ])

    recs = kelly_optimizer.recommend_sizing(trade_closes_path=override)
    assert len(recs) == 1
    assert recs[0]["bot_id"] == "iso"
    assert recs[0]["n_trades"] == 25

