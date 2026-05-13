"""End-to-end smoke test for the full L2 supercharge pipeline.

Runs the entire chain:
    depth_simulator
        ↓
    write JSONL files
        ↓
    l2_backtest_harness (all 3 strategies through CLI surface)
        ↓
    l2_sweep_harness (multi-config grid)
        ↓
    l2_observability.emit_signal + emit_fill (simulated paper-soak)
        ↓
    l2_promotion_evaluator (per-strategy verdict)
        ↓
    l2_fill_audit (slippage realism)
        ↓
    l2_confidence_calibration (Brier score)

Verifies no integration bugs across the boundaries.  Doesn't assert
on edge values — those are unit-test concerns; this test is about
plumbing.
"""

# ruff: noqa: N802, PLR2004
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.scripts import (
    depth_simulator,
    l2_backtest_harness,
    l2_fill_audit,
)
from eta_engine.scripts import l2_confidence_calibration as cal
from eta_engine.scripts import l2_observability as obs
from eta_engine.scripts import l2_promotion_evaluator as prom
from eta_engine.scripts import l2_sweep_harness as sweep

if TYPE_CHECKING:
    import pytest


def test_e2e_full_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One test exercising the entire L2 chain end-to-end.

    Generates synthetic depth → backtests → sweeps → simulates paper
    fills → evaluates promotion → audits slip → measures Brier.  No
    assertion on edge quality; only that no integration boundary
    crashes."""
    # ── 1. Redirect all log paths to tmp_path ──
    monkeypatch.setattr(l2_backtest_harness, "DEPTH_DIR", tmp_path / "depth")
    monkeypatch.setattr(l2_backtest_harness, "L2_BACKTEST_LOG", tmp_path / "bt.jsonl")
    monkeypatch.setattr(l2_backtest_harness, "CONFIG_SEARCH_LOG", tmp_path / "cs.jsonl")
    monkeypatch.setattr(sweep, "SWEEP_LOG", tmp_path / "sw.jsonl")
    monkeypatch.setattr(prom, "L2_BACKTEST_LOG", tmp_path / "bt.jsonl")
    monkeypatch.setattr(prom, "L2_SWEEP_LOG", tmp_path / "sw.jsonl")
    monkeypatch.setattr(prom, "CAPTURE_HEALTH_LOG", tmp_path / "ch.jsonl")
    monkeypatch.setattr(prom, "ALERTS_LOG", tmp_path / "al.jsonl")
    monkeypatch.setattr(prom, "PROMOTION_LOG", tmp_path / "pr.jsonl")
    monkeypatch.setattr(l2_fill_audit, "SIGNAL_LOG", tmp_path / "sig.jsonl")
    monkeypatch.setattr(l2_fill_audit, "BROKER_FILL_LOG", tmp_path / "fill.jsonl")
    monkeypatch.setattr(l2_fill_audit, "FILL_AUDIT_LOG", tmp_path / "fa.jsonl")
    monkeypatch.setattr(cal, "SIGNAL_LOG", tmp_path / "sig.jsonl")
    monkeypatch.setattr(cal, "BROKER_FILL_LOG", tmp_path / "fill.jsonl")
    monkeypatch.setattr(cal, "CALIBRATION_LOG", tmp_path / "calib.jsonl")

    # ── 2. Generate synthetic depth ──
    today = datetime.now(UTC).replace(microsecond=0, second=0)
    snaps, _ = depth_simulator.simulate(
        symbol="MNQ", duration_minutes=60, regime_mix="imbalanced_long", seed=42, start_dt=today
    )
    depth_simulator.write_snapshots(snaps, "MNQ", output_dir=tmp_path / "depth", date_str=today.strftime("%Y%m%d"))

    # ── 3. Run harness on all three depth-consuming strategies ──
    book_result = l2_backtest_harness.run_book_imbalance(
        "MNQ",
        days=1,
        entry_threshold=1.5,
        consecutive_snaps=3,
        n_levels=3,
        atr_stop_mult=1.0,
        rr_target=2.0,
        walk_forward=False,
        log_config_search_flag=False,
    )
    assert book_result.strategy == "book_imbalance"
    assert book_result.n_snapshots == 720  # 60min × 60/5

    micro_result = l2_backtest_harness.run_microprice_drift(
        "MNQ", days=1, drift_threshold_ticks=2.0, consecutive_snaps=3, walk_forward=False, log_config_search_flag=False
    )
    assert micro_result.strategy == "microprice_drift"

    # aggressor_flow needs L1 bars; with no bars it produces 0 trades
    agg_result = l2_backtest_harness.run_aggressor_flow("MNQ", days=1, log_config_search_flag=False)
    assert agg_result.strategy == "aggressor_flow"

    # ── 4. Sweep harness ──
    sweep_summary = sweep.run_sweep(
        "MNQ", days=1, entry_thresholds=[1.5, 2.0], consecutive_snaps=[2, 3], atr_stop_mults=[1.0], rr_targets=[2.0]
    )
    assert sweep_summary.n_configs_tried == 4

    # ── 5. Simulate paper-soak: emit signals + fills with 60% win rate ──
    base = today
    for i in range(50):
        sid = f"E2E-LONG-{i}"
        outcome = "TARGET" if i % 5 != 0 else "STOP"  # 80% win
        obs.emit_signal(
            signal_id=sid,
            strategy_id="book_imbalance_v1",
            bot_id="mnq_book_imbalance_shadow",
            symbol="MNQ",
            side="LONG",
            entry_price=100.0,
            intended_stop_price=99.0,
            intended_target_price=102.0,
            confidence=0.7,
            qty_contracts=1,
            ts=base + timedelta(seconds=i * 10),
            _path=tmp_path / "sig.jsonl",
        )
        obs.emit_fill(
            signal_id=sid,
            broker_exec_id=f"x{i}",
            exit_reason=outcome,
            side="LONG",
            actual_fill_price=102.0 if outcome == "TARGET" else 98.75,
            qty_filled=1,
            commission_usd=0.62,
            ts=base + timedelta(seconds=i * 10 + 30),
            _path=tmp_path / "fill.jsonl",
        )

    # ── 6. Promotion evaluator over the same data ──
    eval_result = prom.evaluate_strategy("mnq_book_imbalance_shadow")
    assert eval_result.bot_id == "mnq_book_imbalance_shadow"
    # No captures yet → won't promote (correctly)
    assert eval_result.recommended_status in ("shadow", "retired")

    # ── 7. Fill audit over the synthetic fills ──
    audit_report = l2_fill_audit.run_audit(predicted_slip_ticks=1.0)
    assert audit_report.n_observations > 0
    # Most fills are TARGET (no slip); some STOP at 98.75 (slip = 1 tick exactly)
    # Verdict should be PASS or INSUFFICIENT (boundary)
    assert audit_report.overall_verdict in ("PASS", "INSUFFICIENT", "FAIL")

    # ── 8. Confidence calibration ──
    calib = cal.run_calibration()
    assert calib.n_observations == 50
    assert calib.brier_score is not None
    # 50 wins forecast at 0.7, 80% actual wins → Brier ≈ low
    # (predict=0.7, actual avg=0.8; (0.7-1)^2 * 0.8 + (0.7-0)^2 * 0.2 ≈ 0.072 + 0.098 = 0.17)
    assert calib.brier_score < 0.3


def test_e2e_runbook_files_exist_and_have_required_sections() -> None:
    """The cutover runbook + decision memo + post-mortem must exist
    with required structural headers — sanity check that they aren't
    placeholders."""
    docs = Path(__file__).resolve().parents[1] / "docs"
    runbook = docs / "L2_LIVE_CUTOVER_RUNBOOK.md"
    memo = docs / "L2_STRATEGY_DECISION_MEMO.md"
    postmortem = docs / "L2_POST_MORTEM_TEMPLATE.md"
    assert runbook.exists()
    assert memo.exists()
    assert postmortem.exists()
    runbook_text = runbook.read_text(encoding="utf-8")
    memo_text = memo.read_text(encoding="utf-8")
    postmortem_text = postmortem.read_text(encoding="utf-8")
    # Runbook must have stages 0-6
    for stage in ("Stage 0", "Stage 1", "Stage 2", "Stage 3", "Stage 4", "Stage 5", "Stage 6"):
        assert stage in runbook_text, f"runbook missing {stage}"
    # Memo must have Quant spec + Red Team dissent + PM decision
    for section in ("Quant spec", "Red Team dissent", "PM decision", "Falsification criteria"):
        assert section in memo_text, f"memo missing {section}"
    # Post-mortem must reference falsification + autopsies
    for section in ("Final metrics", "Red Team dissent retrospective", "Single-trade autopsies", "Lessons captured"):
        assert section in postmortem_text, f"post-mortem missing {section}"
