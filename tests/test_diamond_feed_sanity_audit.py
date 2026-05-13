"""Tests for diamond_feed_sanity_audit (wave-17)."""
# ruff: noqa: N802, PLR2004, SLF001
from __future__ import annotations

import json
from pathlib import Path


def _trade(bot_id: str, fill_price: float | None, pnl: float | None,
           side: str | None = "BUY", idx: int = 0) -> dict:
    """Build a synthetic trade-close record with optional fields."""
    extra: dict = {}
    if fill_price is not None:
        extra["fill_price"] = fill_price
    if pnl is not None:
        extra["realized_pnl"] = pnl
    if side is not None:
        extra["side"] = side
    return {
        "bot_id": bot_id,
        "signal_id": f"{bot_id}_{idx}",
        "ts": f"2026-05-{(idx % 28) + 1:02d}T14:00:00+00:00",
        "realized_r": 0.5,
        "extra": extra,
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


# ────────────────────────────────────────────────────────────────────
# STUCK_PRICE detection
# ────────────────────────────────────────────────────────────────────


def test_stuck_price_flagged_when_zero_variance() -> None:
    """All trades same fill_price -> STUCK_PRICE flag (placeholder feed)."""
    from eta_engine.scripts import diamond_feed_sanity_audit as fs

    trades = [
        _trade("test_bot", fill_price=5.0, pnl=1.0, idx=i)
        for i in range(20)
    ]
    sc = fs._score_bot("test_bot", trades)
    assert sc.verdict == "FLAGGED"
    assert any("STUCK_PRICE" in f for f in sc.flags)


def test_stuck_price_NOT_flagged_when_real_variation() -> None:
    """Realistic fill_price variation passes the STUCK check."""
    from eta_engine.scripts import diamond_feed_sanity_audit as fs

    # 10% variation across 20 trades (e.g., $100 -> $110)
    trades = [
        _trade("test_bot", fill_price=100.0 + i, pnl=1.0, idx=i)
        for i in range(20)
    ]
    sc = fs._score_bot("test_bot", trades)
    assert not any("STUCK_PRICE" in f for f in sc.flags)


# ────────────────────────────────────────────────────────────────────
# ZERO_PNL_ACTIVITY
# ────────────────────────────────────────────────────────────────────


def test_zero_pnl_activity_flagged_when_all_pnl_zero() -> None:
    """All N trades with realized_pnl=0 -> ZERO_PNL_ACTIVITY flag."""
    from eta_engine.scripts import diamond_feed_sanity_audit as fs

    trades = [
        _trade("test_bot", fill_price=100.0 + i, pnl=0.0, idx=i)
        for i in range(20)
    ]
    sc = fs._score_bot("test_bot", trades)
    assert sc.verdict == "FLAGGED"
    assert any("ZERO_PNL_ACTIVITY" in f for f in sc.flags)


def test_zero_pnl_NOT_flagged_when_some_non_zero() -> None:
    """Mixed PnL passes — not every trade needs to be profitable."""
    from eta_engine.scripts import diamond_feed_sanity_audit as fs

    trades = []
    for i in range(20):
        # half zero, half non-zero
        pnl = 0.0 if i % 2 == 0 else (10.0 if i % 4 == 1 else -5.0)
        trades.append(_trade("test_bot", fill_price=100.0 + i, pnl=pnl, idx=i))
    sc = fs._score_bot("test_bot", trades)
    assert not any("ZERO_PNL_ACTIVITY" in f for f in sc.flags)


# ────────────────────────────────────────────────────────────────────
# MISSING_PNL_FIELD / MISSING_SIDE_FIELD
# ────────────────────────────────────────────────────────────────────


def test_missing_pnl_field_flagged_when_majority_absent() -> None:
    from eta_engine.scripts import diamond_feed_sanity_audit as fs

    trades = []
    # 5 with PnL, 15 without → majority missing
    for i in range(5):
        trades.append(_trade("test_bot", fill_price=100.0 + i, pnl=1.0, idx=i))
    for i in range(5, 20):
        trades.append(_trade("test_bot", fill_price=100.0 + i, pnl=None, idx=i))
    sc = fs._score_bot("test_bot", trades)
    assert any("MISSING_PNL_FIELD" in f for f in sc.flags)


def test_missing_side_field_flagged_when_majority_absent() -> None:
    from eta_engine.scripts import diamond_feed_sanity_audit as fs

    trades = []
    for i in range(5):
        trades.append(_trade("test_bot", fill_price=100.0 + i, pnl=1.0,
                              side="BUY", idx=i))
    for i in range(5, 20):
        trades.append(_trade("test_bot", fill_price=100.0 + i, pnl=1.0,
                              side=None, idx=i))
    sc = fs._score_bot("test_bot", trades)
    assert any("MISSING_SIDE_FIELD" in f for f in sc.flags)


# ────────────────────────────────────────────────────────────────────
# CLEAN + INSUFFICIENT_DATA
# ────────────────────────────────────────────────────────────────────


def test_clean_when_all_signals_healthy() -> None:
    from eta_engine.scripts import diamond_feed_sanity_audit as fs

    trades = []
    for i in range(20):
        trades.append(_trade("test_bot", fill_price=100.0 + i,
                              pnl=1.0 if i % 2 == 0 else -0.5,
                              side="BUY" if i % 2 == 0 else "SELL", idx=i))
    sc = fs._score_bot("test_bot", trades)
    assert sc.verdict == "CLEAN"
    assert sc.flags == []


def test_insufficient_data_below_sample_threshold() -> None:
    """Fewer than SAMPLE_THRESHOLD records -> verdict INSUFFICIENT_DATA."""
    from eta_engine.scripts import diamond_feed_sanity_audit as fs

    trades = [
        _trade("test_bot", fill_price=100.0, pnl=1.0, idx=i)
        for i in range(5)
    ]
    sc = fs._score_bot("test_bot", trades)
    assert sc.verdict == "INSUFFICIENT_DATA"


# ────────────────────────────────────────────────────────────────────
# MBT regression case
# ────────────────────────────────────────────────────────────────────


def test_mbt_funding_basis_regression() -> None:
    """The wave-17 motivating case: mbt_funding_basis with fill_price=5.0
    on every trade and realized_pnl=0 across all of them. Must be
    FLAGGED with both STUCK_PRICE and ZERO_PNL_ACTIVITY."""
    from eta_engine.scripts import diamond_feed_sanity_audit as fs

    trades = [
        _trade("mbt_funding_basis", fill_price=5.0, pnl=0.0,
                side="SELL", idx=i)
        for i in range(58)
    ]
    sc = fs._score_bot("mbt_funding_basis", trades)
    assert sc.verdict == "FLAGGED"
    assert any("STUCK_PRICE" in f for f in sc.flags)
    assert any("ZERO_PNL_ACTIVITY" in f for f in sc.flags)


# ────────────────────────────────────────────────────────────────────
# Snapshot file write
# ────────────────────────────────────────────────────────────────────


def test_run_writes_json_receipt(tmp_path: Path, monkeypatch: object) -> None:
    """run() reads the canonical ledger paths and persists a summary."""
    from eta_engine.scripts import diamond_feed_sanity_audit as fs

    can_path = tmp_path / "canonical.jsonl"
    leg_path = tmp_path / "legacy.jsonl"
    _write_jsonl(can_path, [
        _trade("met_sweep_reclaim", fill_price=40.0 + i,
                pnl=1.0 if i % 2 == 0 else -0.5,
                side="BUY", idx=i)
        for i in range(20)
    ])
    _write_jsonl(leg_path, [])
    monkeypatch.setattr(fs, "TRADE_CLOSES_CANONICAL", can_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(fs, "TRADE_CLOSES_LEGACY", leg_path)  # type: ignore[attr-defined]
    out_path = tmp_path / "out.json"
    monkeypatch.setattr(fs, "OUT_LATEST", out_path)  # type: ignore[attr-defined]
    summary = fs.run()

    assert out_path.exists()
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert "ts" in on_disk
    assert "scorecards" in on_disk
    assert "verdict_counts" in on_disk
    assert on_disk["n_audited"] == summary["n_audited"]
