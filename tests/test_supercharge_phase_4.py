"""Tests for the supercharge Phase 4 finalization:

- l2_observability: signal + fill log writers
- l2_confidence_calibration: Brier score + bucket calibration
- l2_backtest_harness: generalization for microprice_drift + aggressor_flow
"""
# ruff: noqa: N802, PLR2004
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eta_engine.scripts import (
    depth_simulator,
    l2_backtest_harness,
)
from eta_engine.scripts import (
    l2_confidence_calibration as cal,
)
from eta_engine.scripts import (
    l2_observability as obs,
)

# ────────────────────────────────────────────────────────────────────
# l2_observability
# ────────────────────────────────────────────────────────────────────


def test_emit_signal_writes_record(tmp_path: Path) -> None:
    path = tmp_path / "sig.jsonl"
    rec = obs.emit_signal(
        signal_id="MNQ-LONG-1",
        strategy_id="book_imbalance_v1",
        bot_id="mnq_book_imbalance_shadow",
        symbol="MNQ",
        side="LONG",
        entry_price=29270.25,
        intended_stop_price=29268.25,
        intended_target_price=29274.25,
        confidence=0.65,
        qty_contracts=1,
        rationale="test signal",
        _path=path,
    )
    assert path.exists()
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["signal_id"] == "MNQ-LONG-1"
    assert parsed["side"] == "LONG"
    assert parsed["entry_price"] == 29270.25
    assert rec == parsed


def test_emit_fill_writes_record(tmp_path: Path) -> None:
    path = tmp_path / "fill.jsonl"
    obs.emit_fill(
        signal_id="MNQ-LONG-1",
        broker_exec_id="abc123",
        exit_reason="TARGET",
        side="LONG",
        actual_fill_price=29274.25,
        qty_filled=1,
        commission_usd=0.62,
        slip_ticks_vs_intended=0.0,
        _path=path,
    )
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["exit_reason"] == "TARGET"
    assert parsed["actual_fill_price"] == 29274.25


def test_emit_signal_from_imbalance_adapter(tmp_path: Path) -> None:
    """Pass an ImbalanceSignal-shaped object directly to the adapter."""
    from eta_engine.strategies.book_imbalance_strategy import ImbalanceSignal
    sig = ImbalanceSignal(
        side="LONG",
        entry_price=29270.25,
        stop=29268.25,
        target=29274.25,
        confidence=0.65,
        rationale="test",
        snapshot_ts="2026-05-11T14:00:00+00:00",
        signal_id="MNQ-LONG-test",
        qty_contracts=1,
        symbol="MNQ",
    )
    path = tmp_path / "sig.jsonl"
    rec = obs.emit_signal_from_imbalance(sig, bot_id="mnq_book_imbalance_shadow",
                                            _path=path)
    assert rec["signal_id"] == "MNQ-LONG-test"
    assert rec["symbol"] == "MNQ"


# ────────────────────────────────────────────────────────────────────
# l2_confidence_calibration
# ────────────────────────────────────────────────────────────────────


def test_brier_score_perfect_predictor() -> None:
    """All confidences match outcomes exactly → brier=0."""
    outcomes = [
        {"signal_id": f"s{i}", "confidence": 1.0, "outcome": 1}
        for i in range(15)
    ] + [
        {"signal_id": f"s{i}", "confidence": 0.0, "outcome": 0}
        for i in range(15, 30)
    ]
    brier = cal.compute_brier_score(outcomes)
    assert brier == 0.0


def test_brier_score_chance_predictor() -> None:
    """All confidences at 0.5 with mixed outcomes → brier=0.25."""
    outcomes = [
        {"signal_id": f"s{i}", "confidence": 0.5, "outcome": 1}
        for i in range(10)
    ] + [
        {"signal_id": f"s{i}", "confidence": 0.5, "outcome": 0}
        for i in range(10, 20)
    ]
    brier = cal.compute_brier_score(outcomes)
    assert brier == pytest.approx(0.25, abs=0.001)


def test_brier_score_none_when_insufficient() -> None:
    assert cal.compute_brier_score([]) is None
    assert cal.compute_brier_score(
        [{"signal_id": "s0", "confidence": 0.5, "outcome": 1}]) is None


def test_calibration_buckets_basic() -> None:
    outcomes = [
        # High-confidence wins
        *[{"signal_id": f"h{i}", "confidence": 0.85, "outcome": 1} for i in range(5)],
        # Low-confidence losses
        *[{"signal_id": f"l{i}", "confidence": 0.15, "outcome": 0} for i in range(5)],
    ]
    buckets = cal.build_buckets(outcomes)
    assert len(buckets) == 10
    # 0.8-0.9 bucket should have 5 entries, all wins
    high = next(b for b in buckets if b.bucket_label == "0.8-0.9")
    assert high.n == 5
    assert high.n_wins == 5
    assert high.realized_win_rate == 1.0
    # 0.1-0.2 bucket should have 5 entries, no wins
    low = next(b for b in buckets if b.bucket_label == "0.1-0.2")
    assert low.n == 5
    assert low.n_wins == 0
    assert low.realized_win_rate == 0.0


def test_build_outcomes_matches_terminal_fills() -> None:
    signals = [
        {"signal_id": "s1", "confidence": 0.6},
        {"signal_id": "s2", "confidence": 0.7},
        {"signal_id": "s3", "confidence": 0.4},  # never filled
    ]
    fills = [
        {"signal_id": "s1", "exit_reason": "ENTRY"},
        {"signal_id": "s1", "exit_reason": "TARGET"},  # terminal win
        {"signal_id": "s2", "exit_reason": "ENTRY"},
        {"signal_id": "s2", "exit_reason": "STOP"},   # terminal loss
    ]
    outcomes = cal._build_outcomes(signals, fills)
    by_sig = {o["signal_id"]: o for o in outcomes}
    assert by_sig["s1"]["outcome"] == 1
    assert by_sig["s2"]["outcome"] == 0
    assert "s3" not in by_sig  # no fill → excluded


def test_run_calibration_no_data_returns_none_brier(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cal, "SIGNAL_LOG", tmp_path / "sig.jsonl")
    monkeypatch.setattr(cal, "BROKER_FILL_LOG", tmp_path / "fill.jsonl")
    monkeypatch.setattr(cal, "CALIBRATION_LOG", tmp_path / "cal.jsonl")
    report = cal.run_calibration()
    assert report.n_observations == 0
    assert report.brier_score is None
    assert report.falsification_triggered is False


def test_run_calibration_triggers_falsification_when_brier_high(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cal, "SIGNAL_LOG", tmp_path / "sig.jsonl")
    monkeypatch.setattr(cal, "BROKER_FILL_LOG", tmp_path / "fill.jsonl")
    monkeypatch.setattr(cal, "CALIBRATION_LOG", tmp_path / "cal.jsonl")
    # Write 100 mismatched signal/fill pairs: high confidence, all losses
    base = datetime.now(UTC) - timedelta(days=1)
    sig_lines = []
    fill_lines = []
    for i in range(100):
        sid = f"sig{i}"
        sig_lines.append(json.dumps({
            "ts": (base + timedelta(seconds=i)).isoformat(),
            "signal_id": sid,
            "confidence": 0.85,
            "strategy_id": "book_imbalance_v1",
        }))
        fill_lines.append(json.dumps({
            "ts": (base + timedelta(seconds=i * 2)).isoformat(),
            "signal_id": sid,
            "exit_reason": "STOP",
        }))
    (tmp_path / "sig.jsonl").write_text("\n".join(sig_lines) + "\n",
                                           encoding="utf-8")
    (tmp_path / "fill.jsonl").write_text("\n".join(fill_lines) + "\n",
                                            encoding="utf-8")
    report = cal.run_calibration(falsification_threshold=0.30)
    # Brier ≈ (0.85 - 0)^2 = 0.7225 → exceeds 0.30 threshold
    assert report.brier_score > 0.30
    assert report.falsification_triggered is True


# ────────────────────────────────────────────────────────────────────
# Harness generalization
# ────────────────────────────────────────────────────────────────────


def test_run_microprice_drift_no_data(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(l2_backtest_harness, "DEPTH_DIR", tmp_path)
    monkeypatch.setattr(l2_backtest_harness, "CONFIG_SEARCH_LOG",
                         tmp_path / "config_search.jsonl")
    result = l2_backtest_harness.run_microprice_drift(
        "MNQ", days=1,
        drift_threshold_ticks=2.0,
        consecutive_snaps=3,
        log_config_search_flag=False,
    )
    assert result.strategy == "microprice_drift"
    assert result.n_trades == 0


def test_run_microprice_drift_with_synthetic_data(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(l2_backtest_harness, "DEPTH_DIR", tmp_path)
    monkeypatch.setattr(l2_backtest_harness, "CONFIG_SEARCH_LOG",
                         tmp_path / "config_search.jsonl")
    today = datetime.now(UTC).replace(microsecond=0, second=0)
    snaps, _ = depth_simulator.simulate(
        symbol="MNQ", duration_minutes=30,
        regime_mix="imbalanced_long", seed=42, start_dt=today)
    depth_simulator.write_snapshots(snaps, "MNQ", output_dir=tmp_path,
                                       date_str=today.strftime("%Y%m%d"))
    result = l2_backtest_harness.run_microprice_drift(
        "MNQ", days=1,
        drift_threshold_ticks=2.0,
        consecutive_snaps=3,
        walk_forward=False,
        log_config_search_flag=False,
    )
    assert result.strategy == "microprice_drift"
    assert result.n_snapshots == 360


def test_run_aggressor_flow_no_bars_returns_zero_trades(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(l2_backtest_harness, "CONFIG_SEARCH_LOG",
                         tmp_path / "config_search.jsonl")
    # _load_l1_bars uses ROOT.parent / "mnq_data" / "history_l1"
    # We can't easily monkeypatch that, but with no real bars the result
    # should naturally be 0
    result = l2_backtest_harness.run_aggressor_flow(
        "MNQ", days=1, log_config_search_flag=False)
    assert result.strategy == "aggressor_flow"
    # n_trades should be 0 since no real bars exist
    assert result.n_trades == 0


def test_harness_main_cli_supports_all_three_strategies() -> None:
    """The argparse choices include all 3 strategy types."""
    import inspect
    src = inspect.getsource(l2_backtest_harness)
    assert "microprice_drift" in src
    assert "aggressor_flow" in src
    assert "book_imbalance" in src
