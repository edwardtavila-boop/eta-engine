from __future__ import annotations

import pytest

from eta_engine.brain.portfolio_rebalancer_v2 import (
    BotPerformance,
    apply_rebalance_plan,
    build_rebalance_plan,
)


def _sample_perf() -> list[BotPerformance]:
    return [
        BotPerformance(
            bot_name="MnqBot",
            rolling_returns=[0.012, 0.011, 0.013, 0.012, 0.014, 0.011, 0.013],
            baseline_usd=5500.0,
        ),
        BotPerformance(
            bot_name="EthPerpBot",
            rolling_returns=[0.004, 0.006, 0.003, 0.005, 0.004, 0.006, 0.003],
            baseline_usd=3000.0,
        ),
        BotPerformance(
            bot_name="GoldBot",
            rolling_returns=[-0.006, 0.002, -0.008, 0.001, -0.005, 0.000, -0.004],
            baseline_usd=1500.0,
        ),
    ]


def test_rebalance_plan_preserves_total_baseline_budget() -> None:
    plan = build_rebalance_plan(_sample_perf())

    assert plan.drawdown_brake_active is False
    assert plan.total_baseline_usd == 10000.0
    assert plan.total_target_usd == pytest.approx(10000.0, abs=0.03)
    assert sum(plan.allocations.values()) == pytest.approx(10000.0, abs=0.03)
    assert {decision.bot_name for decision in plan.decisions} == {"MnqBot", "EthPerpBot", "GoldBot"}


def test_rebalance_plan_dampens_correlated_winner_without_changing_total_budget() -> None:
    plain = build_rebalance_plan(_sample_perf())
    correlated = build_rebalance_plan(_sample_perf(), correlations={("MnqBot", "EthPerpBot"): 0.91})

    assert correlated.total_target_usd == pytest.approx(10000.0, abs=0.03)
    plain_mnq = next(decision for decision in plain.decisions if decision.bot_name == "MnqBot")
    correlated_mnq = next(decision for decision in correlated.decisions if decision.bot_name == "MnqBot")

    assert correlated_mnq.target_usd < plain_mnq.target_usd
    assert correlated_mnq.correlation_group == ("EthPerpBot", "MnqBot")
    assert "correlation_group_damped" in correlated_mnq.reasons


def test_rebalance_plan_drawdown_brake_halves_total_budget() -> None:
    plan = build_rebalance_plan(_sample_perf(), fleet_drawdown_pct=0.06)

    assert plan.drawdown_brake_active is True
    assert plan.total_baseline_usd == 10000.0
    assert plan.total_target_usd == pytest.approx(5000.0, abs=0.03)
    assert all("fleet_drawdown_brake" in decision.reasons for decision in plan.decisions)


def test_apply_rebalance_plan_is_dry_run_by_default() -> None:
    class Bot:
        def __init__(self) -> None:
            self.ceiling: float | None = None

        def set_equity_ceiling(self, usd: float) -> None:
            self.ceiling = usd

    plan = build_rebalance_plan([
        BotPerformance(
            bot_name="MnqBot",
            rolling_returns=[0.012, 0.011, 0.013, 0.012, 0.014],
            baseline_usd=5500.0,
        )
    ])
    bot = Bot()

    dry_run = apply_rebalance_plan({"MnqBot": bot}, plan)
    assert bot.ceiling is None

    applied = apply_rebalance_plan({"MnqBot": bot}, plan, dry_run=False)

    assert dry_run == [{"bot_name": "MnqBot", "target_usd": 5500.0, "status": "dry_run", "dry_run": True}]
    assert applied == [{"bot_name": "MnqBot", "target_usd": 5500.0, "status": "applied", "dry_run": False}]
    assert bot.ceiling == 5500.0
