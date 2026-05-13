"""Tests for diamond_direction_stratify (wave-11)."""

# ruff: noqa: N802, PLR2004, SLF001
from __future__ import annotations

import json
from pathlib import Path


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _trade(bot_id: str, r: float, side: str, idx: int = 0) -> dict:
    """Synthetic trade-close record. ``side`` is the broker side
    (BUY/SELL); the test exercises the wave-10/11 derivation."""
    return {
        "bot_id": bot_id,
        "signal_id": f"{bot_id}_{idx}",
        "ts": f"2026-05-{(idx % 28) + 1:02d}T14:00:00+00:00",
        "realized_r": r,
        "direction": "long",  # broken pre-wave-10 — must be ignored
        "extra": {"side": side},
    }


# ────────────────────────────────────────────────────────────────────
# derive_direction
# ────────────────────────────────────────────────────────────────────


def test_derive_direction_from_extra_side_buy_returns_long() -> None:
    """The canonical pattern: extra.side=BUY -> long."""
    from eta_engine.scripts import diamond_direction_stratify as ds

    rec = {"extra": {"side": "BUY"}, "direction": "long"}
    assert ds.derive_direction(rec) == "long"


def test_derive_direction_from_extra_side_sell_returns_short() -> None:
    """The wave-10 fix: extra.side=SELL -> short.  Pre-wave-10 the
    broken direction='long' field would have been used here."""
    from eta_engine.scripts import diamond_direction_stratify as ds

    rec = {"extra": {"side": "SELL"}, "direction": "long"}
    assert ds.derive_direction(rec) == "short"


def test_derive_direction_extra_side_overrides_direction_field() -> None:
    """When both are present, extra.side wins — protects against the
    pre-wave-10 historical records where direction was always 'long'
    but side carried the truth."""
    from eta_engine.scripts import diamond_direction_stratify as ds

    rec = {"extra": {"side": "SELL"}, "direction": "long"}
    assert ds.derive_direction(rec) == "short"


def test_derive_direction_falls_back_to_top_level_side() -> None:
    """Some pipelines hoist side to the top level."""
    from eta_engine.scripts import diamond_direction_stratify as ds

    rec = {"side": "SELL"}
    assert ds.derive_direction(rec) == "short"


def test_derive_direction_falls_back_to_direction_field_when_no_side() -> None:
    """Post-wave-10 records have correct direction; use it when no side
    is present."""
    from eta_engine.scripts import diamond_direction_stratify as ds

    rec = {"direction": "short"}
    assert ds.derive_direction(rec) == "short"


def test_derive_direction_returns_unknown_when_no_signal() -> None:
    """No side and no usable direction -> 'unknown' (caller handles)."""
    from eta_engine.scripts import diamond_direction_stratify as ds

    assert ds.derive_direction({}) == "unknown"
    assert ds.derive_direction({"direction": "garbage"}) == "unknown"


def test_derive_direction_case_insensitive() -> None:
    from eta_engine.scripts import diamond_direction_stratify as ds

    assert ds.derive_direction({"extra": {"side": "buy"}}) == "long"
    assert ds.derive_direction({"extra": {"side": "Sell"}}) == "short"


# ────────────────────────────────────────────────────────────────────
# Verdict bands
# ────────────────────────────────────────────────────────────────────


def test_verdict_symmetric_when_both_sides_similar_positive() -> None:
    """Both sides positive AND |asymmetry| < DOMINANCE_THRESHOLD_R."""
    from eta_engine.scripts import diamond_direction_stratify as ds

    trades = []
    # 20 long winners +1.0R; 20 short winners +1.05R (similar)
    for i in range(20):
        trades.append(_trade("sym_bot", r=1.0, side="BUY", idx=i))
    for i in range(20, 40):
        trades.append(_trade("sym_bot", r=1.05, side="SELL", idx=i))
    sc = ds._score_bot("sym_bot", trades)
    assert sc.verdict == "SYMMETRIC", sc.rationale


def test_verdict_long_dominant_when_long_avg_clearly_higher() -> None:
    """Long edge >= short edge + DOMINANCE_THRESHOLD_R, both positive."""
    from eta_engine.scripts import diamond_direction_stratify as ds

    trades = []
    # 30 long trades averaging +0.5R; 30 short trades averaging +0.2R
    # Diff = 0.3R > DOMINANCE_THRESHOLD_R (0.10) -> LONG_DOMINANT
    for i in range(30):
        trades.append(_trade("long_dom", r=0.5, side="BUY", idx=i))
    for i in range(30, 60):
        trades.append(_trade("long_dom", r=0.2, side="SELL", idx=i))
    sc = ds._score_bot("long_dom", trades)
    assert sc.verdict == "LONG_DOMINANT", sc.rationale


def test_verdict_short_dominant_when_short_avg_clearly_higher() -> None:
    from eta_engine.scripts import diamond_direction_stratify as ds

    trades = []
    for i in range(30):
        trades.append(_trade("short_dom", r=0.2, side="BUY", idx=i))
    for i in range(30, 60):
        trades.append(_trade("short_dom", r=0.6, side="SELL", idx=i))
    sc = ds._score_bot("short_dom", trades)
    assert sc.verdict == "SHORT_DOMINANT", sc.rationale


def test_verdict_long_only_edge_when_short_negative() -> None:
    """Long positive, short negative -> LONG_ONLY_EDGE
    (operator should consider filtering shorts)."""
    from eta_engine.scripts import diamond_direction_stratify as ds

    trades = []
    for i in range(30):
        trades.append(_trade("long_only", r=0.5, side="BUY", idx=i))
    for i in range(30, 60):
        trades.append(_trade("long_only", r=-0.3, side="SELL", idx=i))
    sc = ds._score_bot("long_only", trades)
    assert sc.verdict == "LONG_ONLY_EDGE"


def test_verdict_short_only_edge_when_long_negative() -> None:
    from eta_engine.scripts import diamond_direction_stratify as ds

    trades = []
    for i in range(30):
        trades.append(_trade("short_only", r=-0.3, side="BUY", idx=i))
    for i in range(30, 60):
        trades.append(_trade("short_only", r=0.5, side="SELL", idx=i))
    sc = ds._score_bot("short_only", trades)
    assert sc.verdict == "SHORT_ONLY_EDGE"


def test_verdict_bidirectional_loss_when_both_negative() -> None:
    """Both sides net-negative -> strategy not working."""
    from eta_engine.scripts import diamond_direction_stratify as ds

    trades = []
    for i in range(30):
        trades.append(_trade("loser", r=-0.4, side="BUY", idx=i))
    for i in range(30, 60):
        trades.append(_trade("loser", r=-0.3, side="SELL", idx=i))
    sc = ds._score_bot("loser", trades)
    assert sc.verdict == "BIDIRECTIONAL_LOSS"


def test_verdict_insufficient_data_under_min_per_direction() -> None:
    """Below MIN_PER_DIRECTION on either side -> INSUFFICIENT_DATA."""
    from eta_engine.scripts import diamond_direction_stratify as ds

    trades = []
    for i in range(30):
        trades.append(_trade("mostly_long", r=0.5, side="BUY", idx=i))
    # Only 5 shorts (< MIN_PER_DIRECTION=10)
    for i in range(30, 35):
        trades.append(_trade("mostly_long", r=0.4, side="SELL", idx=i))
    sc = ds._score_bot("mostly_long", trades)
    assert sc.verdict == "INSUFFICIENT_DATA"


def test_verdict_insufficient_data_when_one_side_zero() -> None:
    """A 100%-long bot (e.g. mnq_futures_sage by design) returns
    INSUFFICIENT_DATA — the asymmetry comparison is undefined."""
    from eta_engine.scripts import diamond_direction_stratify as ds

    trades = [_trade("only_long", r=0.3, side="BUY", idx=i) for i in range(50)]
    sc = ds._score_bot("only_long", trades)
    assert sc.n_long == 50
    assert sc.n_short == 0
    assert sc.verdict == "INSUFFICIENT_DATA"


# ────────────────────────────────────────────────────────────────────
# Snapshot file
# ────────────────────────────────────────────────────────────────────


def test_run_writes_json_receipt(tmp_path: Path, monkeypatch: object) -> None:
    """run() must persist the summary to OUT_LATEST."""
    from eta_engine.scripts import diamond_direction_stratify as ds

    can_path = tmp_path / "canonical.jsonl"
    leg_path = tmp_path / "legacy.jsonl"
    _write_jsonl(
        can_path,
        [_trade("m2k_sweep_reclaim", r=0.5, side="BUY", idx=i) for i in range(20)]
        + [_trade("m2k_sweep_reclaim", r=0.4, side="SELL", idx=i) for i in range(20, 40)],
    )
    _write_jsonl(leg_path, [])
    monkeypatch.setattr(ds, "TRADE_CLOSES_CANONICAL", can_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(ds, "TRADE_CLOSES_LEGACY", leg_path)  # type: ignore[attr-defined]
    out_path = tmp_path / "out.json"
    monkeypatch.setattr(ds, "OUT_LATEST", out_path)  # type: ignore[attr-defined]
    summary = ds.run()

    assert out_path.exists()
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert "ts" in on_disk
    assert "verdict_counts" in on_disk
    assert on_disk["n_diamonds"] == summary["n_diamonds"]
