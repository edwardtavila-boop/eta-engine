"""Regression tests for L2 operator/audit helper scripts."""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.scripts import l2_fill_audit, l2_promotion_evaluator

if TYPE_CHECKING:
    import pytest


def test_l2_fill_audit_no_fills_is_safe_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(l2_fill_audit, "SIGNAL_LOG", tmp_path / "signals.jsonl")
    monkeypatch.setattr(l2_fill_audit, "BROKER_FILL_LOG", tmp_path / "fills.jsonl")
    monkeypatch.setattr(l2_fill_audit, "FILL_AUDIT_LOG", tmp_path / "audit.jsonl")

    report = l2_fill_audit.run_audit()

    assert report.overall_verdict == "NO_FILLS_YET"
    monkeypatch.setattr(sys, "argv", ["l2_fill_audit", "--json"])
    assert l2_fill_audit.main() == 0


def test_l2_fill_audit_slip_is_adverse_for_long_and_short() -> None:
    ts = datetime(2026, 5, 11, 14, 0, tzinfo=UTC).isoformat()
    signals = [
        {
            "signal_id": "long-stop",
            "symbol": "MNQ",
            "side": "LONG",
            "intended_stop_price": 100.0,
            "intended_target_price": 105.0,
        },
        {
            "signal_id": "short-stop",
            "symbol": "MNQ",
            "side": "SHORT",
            "intended_stop_price": 100.0,
            "intended_target_price": 95.0,
        },
    ]
    fills = [
        {
            "signal_id": "long-stop",
            "exit_reason": "STOP",
            "actual_fill_price": 99.5,
            "ts": ts,
        },
        {
            "signal_id": "short-stop",
            "exit_reason": "STOP",
            "actual_fill_price": 100.5,
            "ts": ts,
        },
    ]

    observations = l2_fill_audit._match_signals_to_fills(
        signals,
        fills,
        tick_size=0.25,
    )

    assert [obs.slip_ticks for obs in observations] == [2.0, 2.0]


def test_l2_promotion_harness_strategy_name_removes_exact_suffix_only() -> None:
    assert l2_promotion_evaluator._harness_strategy_name("book_imbalance_v1") == "book_imbalance"
    assert l2_promotion_evaluator._harness_strategy_name("quality_v11") == "quality_v11"
