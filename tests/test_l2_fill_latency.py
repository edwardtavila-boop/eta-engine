"""Tests for l2_fill_latency — measures signal→fill latency vs decay window."""
# ruff: noqa: N802, PLR2004
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eta_engine.scripts import l2_fill_latency as fl


def _write_sig_fill_pair(tmp_path: Path,
                          n: int = 30, latency_seconds: float = 1.0,
                          strategy_id: str = "book_imbalance_v1") -> None:
    sig_path = tmp_path / "sig.jsonl"
    fill_path = tmp_path / "fill.jsonl"
    base = datetime.now(UTC) - timedelta(hours=1)
    sigs = []
    fills = []
    for i in range(n):
        sid = f"sig{i}"
        sig_ts = base + timedelta(seconds=i * 10)
        fill_ts = sig_ts + timedelta(seconds=latency_seconds)
        sigs.append(json.dumps({
            "ts": sig_ts.isoformat(),
            "signal_id": sid,
            "strategy_id": strategy_id,
        }))
        fills.append(json.dumps({
            "ts": fill_ts.isoformat(),
            "signal_id": sid,
            "exit_reason": "ENTRY",
        }))
    sig_path.write_text("\n".join(sigs) + "\n", encoding="utf-8")
    fill_path.write_text("\n".join(fills) + "\n", encoding="utf-8")


def test_fill_latency_no_data_returns_insufficient(tmp_path: Path) -> None:
    report = fl.run_latency_audit(
        _signal_path=tmp_path / "missing.jsonl",
        _fill_path=tmp_path / "missing.jsonl",
    )
    assert report.verdict == "INSUFFICIENT"


def test_fill_latency_ok_when_fast(tmp_path: Path) -> None:
    """1s latency, 5s threshold → OK (well under)."""
    _write_sig_fill_pair(tmp_path, n=30, latency_seconds=1.0)
    report = fl.run_latency_audit(
        strategy_id="book_imbalance_v1",
        _signal_path=tmp_path / "sig.jsonl",
        _fill_path=tmp_path / "fill.jsonl",
    )
    assert report.verdict == "OK"
    assert report.p90_latency_s < 2.0


def test_fill_latency_fail_when_too_slow(tmp_path: Path) -> None:
    """15s latency on microprice (3s threshold) → FAIL."""
    _write_sig_fill_pair(tmp_path, n=30, latency_seconds=15.0,
                           strategy_id="microprice_drift_v1")
    report = fl.run_latency_audit(
        strategy_id="microprice_drift_v1",
        _signal_path=tmp_path / "sig.jsonl",
        _fill_path=tmp_path / "fill.jsonl",
    )
    assert report.verdict == "FAIL"
    assert any("decay threshold" in n for n in report.notes)


def test_fill_latency_marginal_band(tmp_path: Path) -> None:
    """7s latency on book_imbalance (5s threshold) → MARGINAL (1.4×)."""
    _write_sig_fill_pair(tmp_path, n=30, latency_seconds=7.0)
    report = fl.run_latency_audit(
        strategy_id="book_imbalance_v1",
        _signal_path=tmp_path / "sig.jsonl",
        _fill_path=tmp_path / "fill.jsonl",
    )
    assert report.verdict == "MARGINAL"


def test_fill_latency_skips_exit_fills(tmp_path: Path) -> None:
    """Only ENTRY fills should be counted."""
    sig_path = tmp_path / "sig.jsonl"
    fill_path = tmp_path / "fill.jsonl"
    base = datetime.now(UTC) - timedelta(hours=1)
    sigs = []
    fills = []
    for i in range(20):
        sid = f"sig{i}"
        sigs.append(json.dumps({
            "ts": base.isoformat(),
            "signal_id": sid,
            "strategy_id": "book_imbalance_v1",
        }))
        # Two fills per signal: ENTRY + TARGET
        fills.append(json.dumps({
            "ts": (base + timedelta(seconds=1)).isoformat(),
            "signal_id": sid,
            "exit_reason": "ENTRY",
        }))
        fills.append(json.dumps({
            "ts": (base + timedelta(seconds=60)).isoformat(),
            "signal_id": sid,
            "exit_reason": "TARGET",
        }))
    sig_path.write_text("\n".join(sigs) + "\n", encoding="utf-8")
    fill_path.write_text("\n".join(fills) + "\n", encoding="utf-8")
    report = fl.run_latency_audit(
        strategy_id="book_imbalance_v1",
        _signal_path=sig_path, _fill_path=fill_path,
    )
    # Should count only the 20 ENTRY fills, not the 40 total events
    assert report.n_observations == 20


def test_fill_latency_skips_unmatched_signals(tmp_path: Path) -> None:
    """Signals without matching fills are ignored."""
    sig_path = tmp_path / "sig.jsonl"
    fill_path = tmp_path / "fill.jsonl"
    base = datetime.now(UTC) - timedelta(hours=1)
    sig_path.write_text(json.dumps({
        "ts": base.isoformat(),
        "signal_id": "unmatched",
        "strategy_id": "book_imbalance_v1",
    }) + "\n", encoding="utf-8")
    fill_path.write_text("", encoding="utf-8")
    report = fl.run_latency_audit(
        strategy_id="book_imbalance_v1",
        _signal_path=sig_path, _fill_path=fill_path,
    )
    assert report.n_observations == 0


def test_fill_latency_decay_thresholds_per_strategy() -> None:
    """Microprice has tighter threshold than book_imbalance."""
    assert fl.DECAY_THRESHOLDS["microprice_drift_v1"] < \
           fl.DECAY_THRESHOLDS["book_imbalance_v1"]
    assert fl.DECAY_THRESHOLDS["aggressor_flow_v1"] > \
           fl.DECAY_THRESHOLDS["book_imbalance_v1"]


def test_fill_latency_uses_default_threshold_for_unknown_strategy(tmp_path: Path) -> None:
    _write_sig_fill_pair(tmp_path, n=20, latency_seconds=1.0,
                           strategy_id="unknown_v1")
    report = fl.run_latency_audit(
        strategy_id="unknown_v1",
        _signal_path=tmp_path / "sig.jsonl",
        _fill_path=tmp_path / "fill.jsonl",
    )
    assert report.decay_threshold_s == fl.DEFAULT_DECAY_THRESHOLD


def test_fill_latency_multi_symbol_specs_in_harness() -> None:
    """Confirm symbol expansion landed for the harness."""
    from eta_engine.scripts import l2_backtest_harness as h
    for sym in ("MNQ", "NQ", "MES", "ES", "M2K", "RTY", "MYM", "YM",
                 "MGC", "GC", "SIL", "SI", "HG",
                 "MCL", "CL", "QM", "NG", "RB", "HO",
                 "M6E", "6E", "M6B", "6B", "M6J", "6J", "6A", "6C",
                 "ZN", "ZB",
                 "MBT", "BTC", "MET"):
        assert sym in h.SYMBOL_SPECS, f"{sym} missing from SYMBOL_SPECS"
        spec = h.SYMBOL_SPECS[sym]
        assert spec["point_value"] > 0
        assert spec["tick_size"] > 0
        assert spec["default_atr"] > 0


def test_fill_latency_multi_symbol_specs_in_strategy() -> None:
    """Confirm symbol expansion landed for the book_imbalance strategy too."""
    from eta_engine.strategies import book_imbalance_strategy as bis
    for sym in ("MNQ", "NQ", "ES", "M2K", "MYM", "GC", "SIL", "CL",
                 "NG", "ZN", "ZB", "MBT", "BTC", "M6E"):
        assert sym in bis.TICK_SIZE_BY_SYMBOL, f"{sym} missing"
        assert bis.TICK_SIZE_BY_SYMBOL[sym] > 0
