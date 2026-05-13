"""Tests for wave-16 (JARVIS validates himself)."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─── walk_forward_harness.py ──────────────────────────────────────


def test_walk_forward_returns_zero_folds_with_short_data() -> None:
    from eta_engine.brain.jarvis_v3.walk_forward_harness import (
        WalkForwardConfig,
        run_walk_forward,
    )

    rep = run_walk_forward(
        sample_r=[0.5] * 10,
        cfg=WalkForwardConfig(train_size=200, test_size=50),
    )
    assert rep.n_folds == 0


def test_walk_forward_aggregates_metrics() -> None:
    from eta_engine.brain.jarvis_v3.walk_forward_harness import (
        WalkForwardConfig,
        run_walk_forward,
    )

    rng = random.Random(42)
    # Slightly positive distribution
    rs = [rng.gauss(0.2, 1.0) for _ in range(500)]
    rep = run_walk_forward(
        sample_r=rs,
        cfg=WalkForwardConfig(
            train_size=100,
            test_size=50,
            step=50,
            target_sharpe=0.5,
            max_dd_r=20.0,
            min_aggregate_trades=50,
        ),
    )
    assert rep.n_folds >= 5
    assert rep.aggregate_avg_r > 0


def test_walk_forward_passes_when_distribution_strong() -> None:
    from eta_engine.brain.jarvis_v3.walk_forward_harness import (
        WalkForwardConfig,
        run_walk_forward,
    )

    # All winners
    rs = [1.0] * 300
    rep = run_walk_forward(
        sample_r=rs,
        cfg=WalkForwardConfig(
            train_size=100,
            test_size=50,
            step=50,
            target_sharpe=0.5,
            max_dd_r=10.0,
            min_aggregate_trades=50,
            psr_threshold=0.5,
        ),
    )
    assert rep.aggregate_avg_r > 0.5


def test_walk_forward_fails_with_negative_distribution() -> None:
    from eta_engine.brain.jarvis_v3.walk_forward_harness import (
        WalkForwardConfig,
        run_walk_forward,
    )

    rng = random.Random(1)
    rs = [rng.gauss(-0.5, 1.0) for _ in range(300)]
    rep = run_walk_forward(
        sample_r=rs,
        cfg=WalkForwardConfig(train_size=100, test_size=50, step=50),
    )
    assert rep.passed_gates is False


# ─── pre_live_gate.py ────────────────────────────────────────────


def test_pre_live_gate_fails_on_negative_distribution(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.pre_live_gate import evaluate_for_live

    rng = random.Random(2)
    rs = [rng.gauss(-0.5, 1.0) for _ in range(300)]
    decision = evaluate_for_live(
        candidate_id="bad_v1",
        recent_r_multiples=rs,
        replay_lift=0.0,
        regression_pass_rate=1.0,
        decisions_log_path=tmp_path / "decisions.jsonl",
    )
    assert decision.passed is False
    assert decision.failed_gates


def test_pre_live_gate_passes_on_strong_distribution(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.pre_live_gate import (
        PreLiveGateConfig,
        evaluate_for_live,
    )

    rng = random.Random(3)
    # Very strong distribution
    rs = [rng.gauss(1.0, 0.5) for _ in range(300)]
    decision = evaluate_for_live(
        candidate_id="strong_v1",
        recent_r_multiples=rs,
        replay_lift=0.2,
        regression_pass_rate=1.0,
        cfg=PreLiveGateConfig(
            min_sharpe=0.8,
            min_psr=0.5,
            max_drawdown_r=20.0,
            min_trades=50,
            min_is_oos_ratio=0.3,
        ),
        decisions_log_path=tmp_path / "decisions.jsonl",
    )
    assert decision.passed is True


def test_pre_live_gate_persists_decision(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.pre_live_gate import evaluate_for_live

    log_path = tmp_path / "decisions.jsonl"
    evaluate_for_live(
        candidate_id="persist",
        recent_r_multiples=[0.5] * 200,
        decisions_log_path=log_path,
    )
    assert log_path.exists()


# ─── ab_framework.py ─────────────────────────────────────────────


def test_route_signal_deterministic() -> None:
    from eta_engine.brain.jarvis_v3.ab_framework import route_signal

    # Same signal -> same variant
    a = route_signal(signal_id="s1", traffic_split=0.5)
    b = route_signal(signal_id="s1", traffic_split=0.5)
    assert a == b


def test_route_signal_zero_split_always_control() -> None:
    from eta_engine.brain.jarvis_v3.ab_framework import route_signal

    for i in range(20):
        assert (
            route_signal(
                signal_id=f"s{i}",
                traffic_split=0.0,
            )
            == "control"
        )


def test_ab_manager_caps_traffic_split(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.ab_framework import (
        AbBounds,
        AbManager,
    )

    mgr = AbManager(state_path=tmp_path / "ab.json")
    exp = mgr.register_experiment(
        experiment_id="e1",
        traffic_split=0.50,
        bounds=AbBounds(max_traffic_split=0.10),
    )
    # Should cap at 0.10
    assert exp.traffic_split == 0.10


def test_ab_manager_kills_on_single_loss(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.ab_framework import (
        AbBounds,
        AbManager,
    )

    mgr = AbManager(state_path=tmp_path / "ab.json")
    mgr.register_experiment(
        experiment_id="e1",
        traffic_split=0.10,
        bounds=AbBounds(single_loss_kill_r=2.0),
    )
    # Single -3R loss -> kill
    exp = mgr.record_outcome(
        experiment_id="e1",
        variant="treatment",
        realized_r=-3.0,
    )
    assert exp is not None
    assert exp.is_active is False
    assert "single-loss" in exp.killed_reason


def test_ab_manager_records_stats(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.ab_framework import AbManager

    mgr = AbManager(state_path=tmp_path / "ab.json")
    mgr.register_experiment(experiment_id="e2", traffic_split=0.10)
    mgr.record_outcome(
        experiment_id="e2",
        variant="treatment",
        realized_r=1.0,
    )
    mgr.record_outcome(
        experiment_id="e2",
        variant="control",
        realized_r=0.5,
    )
    exp = mgr.get("e2")
    assert exp is not None
    assert exp.treatment.n_trades == 1
    assert exp.control.n_trades == 1


def test_ab_manager_can_declare_winner_with_enough_data(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.ab_framework import AbBounds, AbManager

    mgr = AbManager(state_path=tmp_path / "ab.json")
    mgr.register_experiment(
        experiment_id="e3",
        traffic_split=0.10,
        bounds=AbBounds(
            min_sample_size=10, significance_alpha=0.05, single_loss_kill_r=10.0, cumulative_dd_kill_r=20.0
        ),
    )
    # Treatment consistently better
    rng = random.Random(7)
    for _ in range(40):
        mgr.record_outcome(
            experiment_id="e3",
            variant="treatment",
            realized_r=rng.gauss(1.5, 0.5),
        )
        mgr.record_outcome(
            experiment_id="e3",
            variant="control",
            realized_r=rng.gauss(0.0, 0.5),
        )
    can, winner, p = mgr.can_declare_winner("e3")
    assert can is True
    assert winner == "treatment"
    assert p < 0.05


def test_ab_manager_cannot_declare_with_too_few_samples(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.ab_framework import AbBounds, AbManager

    mgr = AbManager(state_path=tmp_path / "ab.json")
    mgr.register_experiment(
        experiment_id="e4",
        traffic_split=0.10,
        bounds=AbBounds(min_sample_size=30, single_loss_kill_r=10.0),
    )
    for _ in range(5):
        mgr.record_outcome(
            experiment_id="e4",
            variant="treatment",
            realized_r=1.0,
        )
        mgr.record_outcome(
            experiment_id="e4",
            variant="control",
            realized_r=0.0,
        )
    can, winner, p = mgr.can_declare_winner("e4")
    assert can is False


# ─── regression_test_set.py ──────────────────────────────────────


def test_regression_suite_add_and_evaluate_pass_case(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.regression_test_set import (
        CaseKind,
        RegressionSuite,
    )

    suite = RegressionSuite(cases_path=tmp_path / "cases.json")
    suite.add_case(
        case_id="winner1",
        kind=CaseKind.PASS_CASE,
        signal_id="s1",
        proposal_payload={"sage_score": 0.6},
        realized_r=2.0,
        rationale="clean winner",
    )
    # Policy that approves everything -> PASS_CASE passes
    rep = suite.evaluate(policy_fn=lambda _payload: "APPROVED")
    assert rep.n_cases == 1
    assert rep.n_passed == 1
    assert rep.pass_rate == 1.0


def test_regression_suite_detects_regression_on_pass_case(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.regression_test_set import (
        CaseKind,
        RegressionSuite,
    )

    suite = RegressionSuite(cases_path=tmp_path / "cases.json")
    suite.add_case(
        case_id="winner2",
        kind=CaseKind.PASS_CASE,
        signal_id="s2",
        proposal_payload={"x": 1},
        realized_r=2.0,
    )
    # Policy that denies everything -> PASS_CASE regresses
    rep = suite.evaluate(policy_fn=lambda _payload: "DENIED")
    assert rep.n_failed == 1
    assert rep.failed_cases[0].case_id == "winner2"
    assert "regressed" in rep.failed_cases[0].note


def test_regression_suite_detects_regression_on_fail_case(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.regression_test_set import (
        CaseKind,
        RegressionSuite,
    )

    suite = RegressionSuite(cases_path=tmp_path / "cases.json")
    suite.add_case(
        case_id="loser1",
        kind=CaseKind.FAIL_CASE,
        signal_id="s3",
        proposal_payload={"x": 1},
        realized_r=-2.5,
        rationale="catastrophic loss",
    )
    # Policy that approves everything -> FAIL_CASE re-approved (regression)
    rep = suite.evaluate(policy_fn=lambda _payload: "APPROVED")
    assert rep.n_failed == 1
    assert "FAIL_CASE" in rep.failed_cases[0].note


def test_regression_suite_persists_across_instances(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.regression_test_set import (
        CaseKind,
        RegressionSuite,
    )

    path = tmp_path / "cases.json"
    s1 = RegressionSuite(cases_path=path)
    s1.add_case(
        case_id="persist",
        kind=CaseKind.PASS_CASE,
        signal_id="x",
        proposal_payload={"y": 2},
        realized_r=1.5,
    )
    s2 = RegressionSuite(cases_path=path)
    assert len(s2.list_cases()) == 1
    assert s2.list_cases()[0].case_id == "persist"


def test_regression_suite_remove_case(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.regression_test_set import (
        CaseKind,
        RegressionSuite,
    )

    suite = RegressionSuite(cases_path=tmp_path / "cases.json")
    suite.add_case(
        case_id="rm1",
        kind=CaseKind.PASS_CASE,
        signal_id="s",
        proposal_payload={"x": 1},
        realized_r=1.0,
    )
    suite.remove_case("rm1")
    assert len(suite.list_cases()) == 0
