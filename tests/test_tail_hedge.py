"""Tail hedge calculator tests — P4_SHIELD tail_hedge."""

from __future__ import annotations

import pytest

from eta_engine.core.tail_hedge import (
    TailHedgePolicy,
    decide,
    price_inverse_perp_short,
    price_otm_put,
)

# ---------------------------------------------------------------------------
# Black-Scholes sanity
# ---------------------------------------------------------------------------


def test_otm_put_premium_positive() -> None:
    quote = price_otm_put(spy_spot=500.0, otm_pct=10.0, days_to_expiry=30, implied_vol=0.22)
    assert quote["premium_per_share"] > 0
    assert quote["strike"] == pytest.approx(450.0)


def test_deeper_otm_has_lower_premium() -> None:
    shallow = price_otm_put(otm_pct=5.0)
    deep = price_otm_put(otm_pct=20.0)
    assert shallow["premium_per_share"] > deep["premium_per_share"]


def test_higher_vol_has_higher_premium() -> None:
    low = price_otm_put(implied_vol=0.15)
    high = price_otm_put(implied_vol=0.35)
    assert high["premium_per_share"] > low["premium_per_share"]


def test_inverse_perp_short_cost_scales_with_days() -> None:
    short = price_inverse_perp_short(days=7, funding_pct_per_day=0.02)
    long_hold = price_inverse_perp_short(days=30, funding_pct_per_day=0.02)
    assert long_hold["cost_pct"] > short["cost_pct"]


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------


def test_decide_not_armed_when_dd_below_trigger() -> None:
    decision = decide(
        equity_usd=100_000.0,
        current_dd_pct=2.0,
        policy=TailHedgePolicy(trigger_dd_pct=5.0),
    )
    assert decision.armed is False
    assert any("below trigger" in n for n in decision.notes)


def test_decide_arms_when_dd_exceeds_trigger_and_cost_ok() -> None:
    decision = decide(
        equity_usd=1_000_000.0,
        current_dd_pct=8.0,  # beyond 5% trigger
        policy=TailHedgePolicy(trigger_dd_pct=5.0, max_cost_pct_of_equity=2.0),
    )
    assert decision.armed is True
    assert decision.cost_usd > 0
    assert decision.expected_payoff_usd > 0


def test_decide_rejects_when_cost_exceeds_ceiling() -> None:
    # Tiny equity + expensive hedge → cost blows past 0.01% ceiling
    decision = decide(
        equity_usd=1_000.0,
        current_dd_pct=10.0,
        policy=TailHedgePolicy(trigger_dd_pct=5.0, max_cost_pct_of_equity=0.01),
    )
    assert decision.armed is False
    assert any("exceeds ceiling" in n for n in decision.notes)


def test_coverage_pct_tracks_expected_payoff() -> None:
    decision = decide(
        equity_usd=100_000.0,
        current_dd_pct=6.0,
        policy=TailHedgePolicy(trigger_dd_pct=5.0, max_cost_pct_of_equity=10.0),
    )
    # If armed, coverage should be non-zero
    if decision.armed:
        assert decision.coverage_pct > 0


def test_policy_default_kind_reflected_in_decision() -> None:
    d = decide(equity_usd=500_000.0, current_dd_pct=1.0)
    assert d.kind == "otm_put_spy"
