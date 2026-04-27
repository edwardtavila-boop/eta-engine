"""Tests for scripts.monthly_deep_review."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from eta_engine.scripts.monthly_deep_review import run

if TYPE_CHECKING:
    from pathlib import Path


_T0 = datetime(2026, 4, 1, 9, 30, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Helpers to emit JSONL
# --------------------------------------------------------------------------- #


def _trade_row(i: int, *, win: bool) -> dict:
    entry = 100.0
    exit_p = 110.0 if win else 96.0
    return {
        "trade_id": f"t-{i}",
        "symbol": "MNQ",
        "side": "LONG",
        "opened_at": _T0.isoformat(),
        "closed_at": (_T0 + timedelta(minutes=30)).isoformat(),
        "entry_price": entry,
        "exit_price": exit_p,
        "stop_price": 95.0,
        "mfe_price": exit_p,
        "mae_price": 98.0,
        "first_pullback_frac": 0.8,
        "confluence_score": 7.0,
        "regime_at_entry": "TRENDING_UP",
        "gate_overrides": 0,
    }


def _mae_mfe_row(i: int, *, leak: bool) -> dict:
    return {
        "trade_id": f"m-{i}",
        "symbol": "MNQ",
        "side": "LONG",
        "regime": "TRENDING_UP",
        "setup": "fade" if leak else "breakout",
        "opened_at": _T0.isoformat(),
        "closed_at": (_T0 + timedelta(minutes=30)).isoformat(),
        "entry_price": 100.0,
        "exit_price": 103.0 if leak else 110.0,
        "stop_price": 95.0,
        "mfe_price": 115.0 if leak else 110.0,
        "mae_price": 99.0,
        "time_to_mfe_sec": 300.0,
    }


def _rationale_row(i: int, *, losing: bool) -> dict:
    return {
        "trade_id": f"r-{i}",
        "rationale": "fomo chase" if losing else "breakout retest",
        "r_captured": -1.0 if losing else 2.0,
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# run()
# --------------------------------------------------------------------------- #


def test_run_empty_inputs_produces_stub(tmp_path: Path) -> None:
    out_dir = tmp_path / "docs"
    payload = run(out_dir=out_dir, now=_T0)
    assert payload["grading"]["n"] == 0
    assert payload["exit_quality"]["n"] == 0
    assert payload["rationales"]["n"] == 0
    assert (out_dir / "monthly_review_2026_04.json").exists()


def test_run_with_trades_only(tmp_path: Path) -> None:
    trades = tmp_path / "t.jsonl"
    _write_jsonl(trades, [_trade_row(i, win=i % 2 == 0) for i in range(6)])
    out_dir = tmp_path / "docs"
    payload = run(trades_path=trades, out_dir=out_dir, now=_T0)
    assert payload["grading"]["n"] == 6
    assert payload["grading"]["mean_total"] is not None


def test_run_with_mae_mfe_produces_heatmap(tmp_path: Path) -> None:
    mae = tmp_path / "m.jsonl"
    _write_jsonl(mae, [_mae_mfe_row(i, leak=i % 2 == 0) for i in range(4)])
    out_dir = tmp_path / "docs"
    payload = run(mae_mfe_path=mae, out_dir=out_dir, now=_T0)
    assert payload["exit_quality"]["n"] == 4
    assert "heatmap" in payload["exit_quality"]


def test_run_with_rationales_clusters(tmp_path: Path) -> None:
    ra = tmp_path / "r.jsonl"
    rows = []
    for i in range(3):
        rows.append(_rationale_row(i, losing=True))
    for i in range(3, 6):
        rows.append(_rationale_row(i, losing=False))
    _write_jsonl(ra, rows)
    out_dir = tmp_path / "docs"
    payload = run(rationales_path=ra, out_dir=out_dir, now=_T0)
    assert payload["rationales"]["n"] == 6


def test_run_full_pipeline_proposes_tweaks(tmp_path: Path) -> None:
    trades = tmp_path / "t.jsonl"
    mae = tmp_path / "m.jsonl"
    ra = tmp_path / "r.jsonl"
    _write_jsonl(trades, [_trade_row(i, win=False) for i in range(5)])
    _write_jsonl(mae, [_mae_mfe_row(i, leak=True) for i in range(6)])
    _write_jsonl(
        ra,
        [_rationale_row(i, losing=True) for i in range(3)] + [_rationale_row(i, losing=False) for i in range(3, 6)],
    )
    out_dir = tmp_path / "docs"
    payload = run(
        trades_path=trades,
        mae_mfe_path=mae,
        rationales_path=ra,
        out_dir=out_dir,
        now=_T0,
    )
    assert len(payload["proposed_tweaks"]) > 0
    assert len(payload["proposed_tweaks"]) <= 3


def test_run_writes_latest_copies(tmp_path: Path) -> None:
    out_dir = tmp_path / "docs"
    run(out_dir=out_dir, now=_T0)
    assert (out_dir / "monthly_review_latest.json").exists()
    assert (out_dir / "monthly_review_latest.txt").exists()


def test_malformed_jsonl_skipped(tmp_path: Path) -> None:
    trades = tmp_path / "t.jsonl"
    rows = [json.dumps(_trade_row(0, win=True)), "this is not json", json.dumps(_trade_row(1, win=False))]
    trades.write_text("\n".join(rows) + "\n", encoding="utf-8")
    out_dir = tmp_path / "docs"
    payload = run(trades_path=trades, out_dir=out_dir, now=_T0)
    assert payload["grading"]["n"] == 2  # the malformed row was skipped
