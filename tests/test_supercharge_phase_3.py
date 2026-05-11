"""Tests for the supercharge Phase 3 tooling:

- l2_sweep_harness: multi-config sweep with deflated sharpe
- l2_promotion_evaluator: per-strategy promotion verdict
- l2_fill_audit: realized vs predicted slippage
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
    l2_fill_audit,
)
from eta_engine.scripts import (
    l2_promotion_evaluator as prom,
)
from eta_engine.scripts import (
    l2_sweep_harness as sweep,
)

# ────────────────────────────────────────────────────────────────────
# l2_sweep_harness
# ────────────────────────────────────────────────────────────────────


def test_sweep_runs_default_grid_size(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Default grid is 3 × 3 × 2 × 2 = 36 configs.  Each run should
    populate the results list.  Empty depth dir → 0 trades each."""
    monkeypatch.setattr(l2_backtest_harness, "DEPTH_DIR", tmp_path)
    # Suppress config-search log to keep tests isolated
    monkeypatch.setattr(l2_backtest_harness, "CONFIG_SEARCH_LOG",
                          tmp_path / "config_search.jsonl")
    monkeypatch.setattr(sweep, "SWEEP_LOG", tmp_path / "sweep.jsonl")
    summary = sweep.run_sweep("MNQ", days=1)
    assert summary.n_configs_tried == 36
    assert summary.n_configs_valid == 0  # no trades → no valid sharpe
    assert len(summary.results) == 36
    assert summary.promotion_gate_passes is False
    # Warnings should flag the under-sampling
    assert any("NO CONFIG" in w for w in summary.warnings)


def test_sweep_custom_grid_size(tmp_path: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(l2_backtest_harness, "DEPTH_DIR", tmp_path)
    monkeypatch.setattr(l2_backtest_harness, "CONFIG_SEARCH_LOG",
                          tmp_path / "config_search.jsonl")
    monkeypatch.setattr(sweep, "SWEEP_LOG", tmp_path / "sweep.jsonl")
    summary = sweep.run_sweep(
        "MNQ", days=1,
        entry_thresholds=[1.5, 2.0],
        consecutive_snaps=[3],
        atr_stop_mults=[1.0],
        rr_targets=[2.0],
    )
    assert summary.n_configs_tried == 2


def test_sweep_with_real_synthetic_data_ranks_configs(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Generate enough synthetic depth that some configs fire, then
    verify the sweep ranks them (best first)."""
    monkeypatch.setattr(l2_backtest_harness, "DEPTH_DIR", tmp_path)
    monkeypatch.setattr(l2_backtest_harness, "CONFIG_SEARCH_LOG",
                          tmp_path / "config_search.jsonl")
    monkeypatch.setattr(sweep, "SWEEP_LOG", tmp_path / "sweep.jsonl")
    today = datetime.now(UTC).replace(microsecond=0, second=0)
    snaps, _ = depth_simulator.simulate(
        symbol="MNQ", duration_minutes=60,
        regime_mix="imbalanced_long", seed=42, start_dt=today)
    depth_simulator.write_snapshots(snaps, "MNQ", output_dir=tmp_path,
                                       date_str=today.strftime("%Y%m%d"))
    summary = sweep.run_sweep(
        "MNQ", days=1,
        entry_thresholds=[1.5, 2.0],
        consecutive_snaps=[2, 3],
        atr_stop_mults=[1.0],
        rr_targets=[2.0],
    )
    assert summary.n_configs_tried == 4
    # Some configs should fire at least 1 trade on imbalanced data
    assert any(r.n_trades > 0 for r in summary.results)


# ────────────────────────────────────────────────────────────────────
# l2_promotion_evaluator
# ────────────────────────────────────────────────────────────────────


def test_promotion_evaluator_with_no_data_returns_no_promotion(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With empty logs, no strategy should be recommended for promotion."""
    monkeypatch.setattr(prom, "L2_BACKTEST_LOG", tmp_path / "bt.jsonl")
    monkeypatch.setattr(prom, "L2_SWEEP_LOG", tmp_path / "sw.jsonl")
    monkeypatch.setattr(prom, "CAPTURE_HEALTH_LOG", tmp_path / "ch.jsonl")
    monkeypatch.setattr(prom, "ALERTS_LOG", tmp_path / "al.jsonl")
    monkeypatch.setattr(prom, "PROMOTION_LOG", tmp_path / "pr.jsonl")
    evals = prom.evaluate_all()
    assert len(evals) >= 1
    # All should stay in their current status (shadow)
    for e in evals:
        assert e.recommended_status == e.current_status


def test_promotion_evaluator_evaluates_specific_strategy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prom, "L2_BACKTEST_LOG", tmp_path / "bt.jsonl")
    monkeypatch.setattr(prom, "L2_SWEEP_LOG", tmp_path / "sw.jsonl")
    monkeypatch.setattr(prom, "CAPTURE_HEALTH_LOG", tmp_path / "ch.jsonl")
    monkeypatch.setattr(prom, "ALERTS_LOG", tmp_path / "al.jsonl")
    e = prom.evaluate_strategy("mnq_book_imbalance_shadow")
    assert e.bot_id == "mnq_book_imbalance_shadow"
    assert e.current_status == "shadow"


def test_promotion_evaluator_unknown_strategy_raises() -> None:
    with pytest.raises(ValueError):
        prom.evaluate_strategy("nonexistent_strategy")


def test_promotion_evaluator_retirement_triggers_on_negative_sharpe(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When a strategy's recent backtest shows OOS sharpe < 0 + sharpe
    CI entirely negative, evaluator recommends retirement."""
    monkeypatch.setattr(prom, "L2_BACKTEST_LOG", tmp_path / "bt.jsonl")
    monkeypatch.setattr(prom, "L2_SWEEP_LOG", tmp_path / "sw.jsonl")
    monkeypatch.setattr(prom, "CAPTURE_HEALTH_LOG", tmp_path / "ch.jsonl")
    monkeypatch.setattr(prom, "ALERTS_LOG", tmp_path / "al.jsonl")
    # Write a backtest record with bad OOS metrics
    bad_record = {
        "ts": datetime.now(UTC).isoformat(),
        "strategy": "book_imbalance",
        "symbol": "MNQ",
        "n_trades": 50,
        "sharpe_proxy": -0.8,
        "sharpe_ci_95": [-2.0, -0.3],
        "walk_forward": {
            "test": {"sharpe_proxy": -0.6, "n_trades": 15, "win_rate": 0.30,
                      "total_pnl_dollars_net": -200.0, "sharpe_proxy_valid": False},
            "promotion_gate": {"passes": False},
        },
    }
    with (tmp_path / "bt.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(bad_record) + "\n")
    e = prom.evaluate_strategy("mnq_book_imbalance_shadow")
    assert e.recommended_status == "retired"
    assert any("Retirement triggered" in n for n in e.notes)


# ────────────────────────────────────────────────────────────────────
# l2_fill_audit
# ────────────────────────────────────────────────────────────────────


def test_fill_audit_no_data_returns_no_fills_yet(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(l2_fill_audit, "SIGNAL_LOG", tmp_path / "sig.jsonl")
    monkeypatch.setattr(l2_fill_audit, "BROKER_FILL_LOG", tmp_path / "fill.jsonl")
    monkeypatch.setattr(l2_fill_audit, "FILL_AUDIT_LOG", tmp_path / "audit.jsonl")
    report = l2_fill_audit.run_audit()
    assert report.overall_verdict == "NO_FILLS_YET"


def test_fill_audit_session_bucket_classifies_rth_correctly() -> None:
    # 13:45 UTC = 15 min into RTH (RTH opens 13:30 UTC) → RTH_OPEN
    dt = datetime(2026, 5, 11, 13, 45, 0, tzinfo=UTC)
    assert l2_fill_audit._session_bucket(dt) == "RTH_OPEN"
    # 16:00 UTC = midday
    dt = datetime(2026, 5, 11, 16, 0, 0, tzinfo=UTC)
    assert l2_fill_audit._session_bucket(dt) == "RTH_MID"
    # 19:45 UTC = last 15 min before close (RTH close at 20:00)
    dt = datetime(2026, 5, 11, 19, 45, 0, tzinfo=UTC)
    assert l2_fill_audit._session_bucket(dt) == "RTH_CLOSE"
    # 02:00 UTC = overnight
    dt = datetime(2026, 5, 11, 2, 0, 0, tzinfo=UTC)
    assert l2_fill_audit._session_bucket(dt) == "ETH"


def test_fill_audit_signed_slip_long_stop_adverse() -> None:
    """LONG stop at 100.0, actual fill 99.50 → 2 ticks of adverse slip."""
    slip = l2_fill_audit._signed_slip_ticks(
        intended=100.0, actual=99.50, side="LONG",
        exit_reason="STOP", tick_size=0.25)
    assert slip == 2.0  # (100.0 - 99.50) / 0.25 = 2 ticks worse


def test_fill_audit_signed_slip_short_stop_adverse() -> None:
    """SHORT stop at 100.0, actual fill 100.50 → 2 ticks adverse."""
    slip = l2_fill_audit._signed_slip_ticks(
        intended=100.0, actual=100.50, side="SHORT",
        exit_reason="STOP", tick_size=0.25)
    assert slip == 2.0


def test_fill_audit_signed_slip_zero_when_perfect_fill() -> None:
    slip = l2_fill_audit._signed_slip_ticks(
        intended=100.0, actual=100.0, side="LONG",
        exit_reason="TARGET", tick_size=0.25)
    assert slip == 0.0


def test_fill_audit_matches_signals_and_fills(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(l2_fill_audit, "SIGNAL_LOG", tmp_path / "sig.jsonl")
    monkeypatch.setattr(l2_fill_audit, "BROKER_FILL_LOG", tmp_path / "fill.jsonl")
    monkeypatch.setattr(l2_fill_audit, "FILL_AUDIT_LOG", tmp_path / "audit.jsonl")
    now = datetime.now(UTC).replace(hour=16, minute=0, second=0, microsecond=0)
    # Write a signal + matching fill (RTH_MID bucket)
    signal = {
        "ts": now.isoformat(),
        "signal_id": "MNQ-LONG-test1",
        "symbol": "MNQ",
        "side": "LONG",
        "intended_stop_price": 100.0,
        "intended_target_price": 102.0,
    }
    fill = {
        "ts": now.isoformat(),
        "signal_id": "MNQ-LONG-test1",
        "exit_reason": "STOP",
        "side": "LONG",
        "actual_fill_price": 99.50,  # 2 ticks adverse
    }
    with (tmp_path / "sig.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(signal) + "\n")
    with (tmp_path / "fill.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(fill) + "\n")
    report = l2_fill_audit.run_audit()
    assert report.n_observations == 1


def test_fill_audit_realism_verdict_fail_on_high_slip(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(l2_fill_audit, "SIGNAL_LOG", tmp_path / "sig.jsonl")
    monkeypatch.setattr(l2_fill_audit, "BROKER_FILL_LOG", tmp_path / "fill.jsonl")
    monkeypatch.setattr(l2_fill_audit, "FILL_AUDIT_LOG", tmp_path / "audit.jsonl")
    now = datetime.now(UTC).replace(hour=16, minute=0, second=0, microsecond=0)
    # 10 RTH_MID fills all with 5 ticks slip — far worse than 1-tick predicted
    sig_lines = []
    fill_lines = []
    for i in range(10):
        sid = f"MNQ-LONG-test{i}"
        ts = (now + timedelta(seconds=i * 5)).isoformat()
        sig_lines.append(json.dumps({
            "ts": ts, "signal_id": sid, "symbol": "MNQ", "side": "LONG",
            "intended_stop_price": 100.0, "intended_target_price": 102.0,
        }))
        fill_lines.append(json.dumps({
            "ts": ts, "signal_id": sid, "exit_reason": "STOP",
            "side": "LONG", "actual_fill_price": 98.75,  # 5 ticks adverse
        }))
    (tmp_path / "sig.jsonl").write_text("\n".join(sig_lines) + "\n",
                                          encoding="utf-8")
    (tmp_path / "fill.jsonl").write_text("\n".join(fill_lines) + "\n",
                                           encoding="utf-8")
    report = l2_fill_audit.run_audit(predicted_slip_ticks=1.0)
    # RTH_MID bucket should FAIL (5 ticks > 2× 1 tick = 2 ticks)
    rth_mid = next((b for b in report.buckets if b.session == "RTH_MID"), None)
    assert rth_mid is not None
    assert rth_mid.realism_verdict == "FAIL"
    assert report.overall_verdict == "FAIL"
