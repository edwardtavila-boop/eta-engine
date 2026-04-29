from __future__ import annotations

from eta_engine.scripts.fleet_strategy_optimizer import (
    Candidate,
    CellRunResult,
    _build_plans,
    _rank,
)


def _result(
    label: str,
    *,
    is_sharpe: float,
    oos_sharpe: float,
    dsr_pass: float,
    degradation: float,
    positive_oos: int,
    windows: int = 10,
    pass_gate: bool = False,
) -> CellRunResult:
    return CellRunResult(
        bot_id="eth_perp",
        candidate=Candidate("crypto_orb", label, {}),
        n_windows=windows,
        n_positive_oos=positive_oos,
        agg_is_sharpe=is_sharpe,
        agg_oos_sharpe=oos_sharpe,
        avg_oos_degradation=degradation,
        fold_dsr_median=0.5,
        fold_dsr_pass_fraction=dsr_pass,
        pass_gate=pass_gate,
    )


def test_rank_prefers_passed_cells_over_robust_failures() -> None:
    robust_fail = _result(
        "robust_fail",
        is_sharpe=1.0,
        oos_sharpe=3.0,
        dsr_pass=0.7,
        degradation=0.2,
        positive_oos=7,
    )
    weak_pass = _result(
        "weak_pass",
        is_sharpe=0.2,
        oos_sharpe=0.5,
        dsr_pass=0.51,
        degradation=0.3,
        positive_oos=6,
        pass_gate=True,
    )

    assert _rank([robust_fail, weak_pass])[0].candidate.label == "weak_pass"


def test_rank_demotes_runaway_oos_when_is_is_negative() -> None:
    runaway = _result(
        "runaway_oos_negative_is",
        is_sharpe=-0.7,
        oos_sharpe=64.0,
        dsr_pass=0.7,
        degradation=0.1,
        positive_oos=8,
    )
    robust = _result(
        "robust_fail",
        is_sharpe=0.1,
        oos_sharpe=3.0,
        dsr_pass=0.6,
        degradation=0.3,
        positive_oos=6,
    )

    assert _rank([runaway, robust])[0].candidate.label == "robust_fail"


def test_optimizer_includes_registered_mnq_orb_anchor() -> None:
    plan = next(plan for plan in _build_plans() if plan.bot_id == "mnq_futures")

    registered = next(
        candidate for candidate in plan.candidates
        if candidate.label == "registered mnq_orb_v2"
    )
    assert registered.kind == "orb"
    assert registered.cfg == {
        "range_minutes": 5,
        "rr_target": 3.0,
        "atr_stop_mult": 1.5,
        "ema_bias_period": 50,
    }


def test_optimizer_includes_registered_eth_crypto_orb_anchor() -> None:
    plan = next(plan for plan in _build_plans() if plan.bot_id == "eth_perp")

    registered = next(
        candidate for candidate in plan.candidates
        if candidate.label == "registered eth_corb_v4"
    )
    assert registered.kind == "crypto_orb"
    assert registered.cfg == {
        "range_minutes": 180,
        "atr_stop_mult": 1.5,
        "rr_target": 1.0,
    }


def test_optimizer_includes_registered_sol_crypto_orb_anchor_with_trade_cap() -> None:
    plan = next(plan for plan in _build_plans() if plan.bot_id == "sol_perp")

    registered = next(
        candidate for candidate in plan.candidates
        if candidate.label == "registered sol_corb_v2"
    )
    assert registered.kind == "crypto_orb"
    assert registered.cfg == {
        "range_minutes": 240,
        "max_trades_per_day": 1,
        "atr_stop_mult": 1.5,
        "rr_target": 2.0,
    }
