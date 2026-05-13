"""Prop-account risk guard tests.

These tests protect the paper-to-prop lane from "looks small but can
liquidate the account" sizing errors. The governor runs before a prop
venue sees a new entry.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from eta_engine.scripts.broker_router import PendingOrder
from eta_engine.scripts.prop_risk_governor import (
    estimate_bracket_risk_usd,
    evaluate_prop_order,
)


def _order(**overrides: object) -> PendingOrder:
    payload = {
        "ts": "2026-05-12T13:00:00+00:00",
        "signal_id": "sig-prop",
        "side": "BUY",
        "qty": 1.0,
        "symbol": "MNQ",
        "limit_price": 25_000.0,
        "bot_id": "volume_profile_mnq",
        "stop_price": 24_900.0,
        "target_price": 25_100.0,
        "reduce_only": False,
    }
    payload.update(overrides)
    return PendingOrder(**payload)  # type: ignore[arg-type]


def test_mnq_bracket_risk_uses_contract_multiplier() -> None:
    risk = estimate_bracket_risk_usd(
        symbol="MNQM6",
        qty=2,
        entry_price=25_000,
        stop_price=24_900,
    )

    assert risk == 400.0


def test_allows_entry_inside_daily_and_trailing_buffer() -> None:
    verdict = evaluate_prop_order(
        _order(qty=1),
        {
            "alias": "blusky_50k",
            "starting_balance_usd": "50000",
            "current_equity_usd": "50500",
            "peak_equity_usd": "50500",
            "trailing_drawdown_usd": "2500",
            "daily_loss_limit_usd": "1500",
            "daily_loss_used_usd": "0",
            "liquidation_buffer_usd": "500",
            "max_order_risk_usd": "250",
        },
    )

    assert verdict.allow is True
    assert verdict.context["risk_usd"] == 200.0


def test_launch_50k_phase1_allows_only_tiny_mnq_entry_risk() -> None:
    verdict = evaluate_prop_order(
        _order(qty=1, limit_price=25_000, stop_price=24_950),
        {
            "alias": "blusky_launch_50k_phase1",
            "starting_balance_usd": "50000",
            "current_equity_usd": "50000",
            "peak_equity_usd": "50000",
            "trailing_drawdown_usd": "2000",
            "daily_loss_limit_usd": "500",
            "daily_loss_used_usd": "0",
            "liquidation_buffer_usd": "300",
            "max_order_risk_usd": "100",
            "target_profit_usd": "3000",
            "consistency_max_day_profit_pct_of_target": "55",
            "consistency_buffer_usd": "150",
        },
    )

    assert verdict.allow is True
    assert verdict.context["risk_usd"] == 100.0
    assert verdict.context["trailing_floor_usd"] == 48000.0
    assert verdict.context["consistency_profit_cap_usd"] == 1650.0


def test_launch_50k_phase1_blocks_wide_mnq_stop_before_prop_route() -> None:
    verdict = evaluate_prop_order(
        _order(qty=1, limit_price=25_000, stop_price=24_900),
        {
            "alias": "blusky_launch_50k_phase1",
            "starting_balance_usd": "50000",
            "current_equity_usd": "50000",
            "peak_equity_usd": "50000",
            "trailing_drawdown_usd": "2000",
            "daily_loss_limit_usd": "500",
            "daily_loss_used_usd": "0",
            "liquidation_buffer_usd": "300",
            "max_order_risk_usd": "100",
        },
    )

    assert verdict.allow is False
    assert verdict.reason == "prop_risk_exceeds_headroom"
    assert verdict.context["risk_usd"] == 200.0
    assert verdict.context["usable_entry_room_usd"] == 100.0


def test_launch_50k_phase1_blocks_near_consistency_profit_cap() -> None:
    verdict = evaluate_prop_order(
        _order(qty=1, limit_price=25_000, stop_price=24_975),
        {
            "alias": "blusky_launch_50k_phase1",
            "starting_balance_usd": "50000",
            "current_equity_usd": "51500",
            "peak_equity_usd": "51500",
            "trailing_drawdown_usd": "2000",
            "daily_loss_limit_usd": "500",
            "daily_loss_used_usd": "0",
            "liquidation_buffer_usd": "300",
            "max_order_risk_usd": "100",
            "target_profit_usd": "3000",
            "consistency_max_day_profit_pct_of_target": "55",
            "consistency_buffer_usd": "150",
            "daily_realized_pnl_usd": "1500",
        },
    )

    assert verdict.allow is False
    assert verdict.reason == "prop_consistency_profit_cap_reached"
    assert verdict.context["daily_profit_usd"] == 1500.0
    assert verdict.context["consistency_room_usd"] == 0.0


def test_launch_50k_phase1_routing_profile_matches_portal_rules() -> None:
    routing = Path(__file__).resolve().parents[1] / "configs" / "bot_broker_routing.yaml"
    data = yaml.safe_load(routing.read_text(encoding="utf-8"))
    account = data["prop_accounts"]["blusky_launch_50k_phase1"]

    assert account["account_label"] == "BSKELAUNCHEDWARD15586"
    assert account["starting_balance_usd"] == 50000
    assert account["max_drawdown_floor_usd"] == 48000
    assert account["trailing_drawdown_usd"] == 2000
    assert account["target_profit_usd"] == 3000
    assert account["daily_loss_limit_usd"] == 500
    assert account["max_order_risk_usd"] == 100

    verdict = evaluate_prop_order(
        _order(qty=1, limit_price=25_000, stop_price=24_950),
        account,
        env={
            "BLUSKY_LAUNCH_50K_CURRENT_EQUITY_USD": "50000",
            "BLUSKY_LAUNCH_50K_PEAK_EQUITY_USD": "50000",
            "BLUSKY_LAUNCH_50K_DAILY_LOSS_USED_USD": "0",
            "BLUSKY_LAUNCH_50K_OPEN_RISK_USD": "0",
            "BLUSKY_LAUNCH_50K_DAILY_REALIZED_PNL_USD": "0",
        },
    )

    assert verdict.allow is True
    assert verdict.context["usable_entry_room_usd"] == 100.0


def test_blocks_entry_when_trailing_drawdown_room_is_too_small() -> None:
    verdict = evaluate_prop_order(
        _order(qty=1),
        {
            "alias": "blusky_50k",
            "starting_balance_usd": "50000",
            "current_equity_usd": "50100",
            "peak_equity_usd": "52400",
            "trailing_drawdown_usd": "2500",
            "daily_loss_limit_usd": "1500",
            "daily_loss_used_usd": "0",
            "liquidation_buffer_usd": "500",
            "max_order_risk_usd": "250",
        },
    )

    assert verdict.allow is False
    assert verdict.reason == "prop_risk_exceeds_headroom"
    assert verdict.context["trailing_room_usd"] < 0


def test_missing_live_equity_blocks_fail_closed() -> None:
    verdict = evaluate_prop_order(
        _order(qty=1),
        {
            "alias": "blusky_50k",
            "starting_balance_usd": "50000",
            "current_equity_env": "BLUSKY_CURRENT_EQUITY_USD",
            "peak_equity_env": "BLUSKY_PEAK_EQUITY_USD",
            "trailing_drawdown_usd": "2500",
            "daily_loss_limit_usd": "1500",
            "liquidation_buffer_usd": "500",
        },
    )

    assert verdict.allow is False
    assert verdict.reason == "prop_risk_rule_incomplete"
    assert "current_equity_usd" in verdict.context["missing_fields"]


def test_reduce_only_exit_is_allowed_without_entry_risk() -> None:
    verdict = evaluate_prop_order(
        _order(reduce_only=True, stop_price=None, target_price=None),
        {
            "alias": "blusky_50k",
            "starting_balance_usd": "50000",
        },
    )

    assert verdict.allow is True
    assert verdict.reason == "reduce_only_exit"
