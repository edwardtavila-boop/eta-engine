from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.scripts import reconcile_ibkr_position_mismatches as mod


def _payload() -> dict:
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "broker_only": [
            {"symbol": "MCL", "broker_qty": 1.0},
            {"symbol": "MYM", "broker_qty": 1.0},
        ],
        "divergent": [
            {"symbol": "MNQ", "broker_qty": 3.0, "supervisor_qty": 1.0, "delta": 2.0},
        ],
    }


def test_build_plans_closes_broker_only_and_only_excess_divergence() -> None:
    plans = mod.build_plans(_payload())

    assert [plan.symbol for plan in plans] == ["MCL", "MYM", "MNQ"]
    assert [plan.category for plan in plans] == ["broker_only", "broker_only", "divergent_excess"]
    assert [(plan.action, plan.quantity) for plan in plans] == [("SELL", 1.0), ("SELL", 1.0), ("SELL", 2.0)]
    assert plans[-1].supervisor_qty == 1.0


def test_build_plans_can_filter_symbols_and_categories() -> None:
    plans = mod.build_plans(_payload(), symbols={"MNQ"}, include_broker_only=True, include_divergent=True)
    assert [plan.symbol for plan in plans] == ["MNQ"]

    broker_only = mod.build_plans(_payload(), include_broker_only=True, include_divergent=False)
    assert [plan.symbol for plan in broker_only] == ["MCL", "MYM"]

    divergent = mod.build_plans(_payload(), include_broker_only=False, include_divergent=True)
    assert [plan.symbol for plan in divergent] == ["MNQ"]


def test_assert_reconcile_fresh_blocks_stale_artifacts() -> None:
    payload = _payload()
    payload["checked_at"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()

    with pytest.raises(RuntimeError, match="stale"):
        mod.assert_reconcile_fresh(payload, max_age_s=60)


def test_validate_plan_blocks_side_changes_and_overclose() -> None:
    plan = mod.build_plans(_payload(), symbols={"MNQ"})[0]

    mod._validate_plan_against_broker(plan, 3.0)
    with pytest.raises(RuntimeError, match="smaller"):
        mod._validate_plan_against_broker(plan, 1.0)
    with pytest.raises(RuntimeError, match="side changed"):
        mod._validate_plan_against_broker(plan, -3.0)
