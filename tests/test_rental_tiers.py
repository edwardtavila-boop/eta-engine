"""
EVOLUTIONARY TRADING ALGO  //  tests.test_rental_tiers
==========================================
Catalog + pricing lookup tests for the rental SaaS tier module.
"""

from __future__ import annotations

import pytest

from eta_engine.rental.tiers import (
    DEFAULT_CATALOG,
    ELITE,
    PORTFOLIO,
    PRO,
    STARTER,
    TRIAL,
    BotSku,
    RentalTier,
    TierCatalog,
    price_for,
)

# ---------------------------------------------------------------------------
# Catalog shape
# ---------------------------------------------------------------------------


def test_default_catalog_has_five_tiers() -> None:
    ids = [t.id for t in DEFAULT_CATALOG.tiers]
    assert ids == [
        RentalTier.TRIAL,
        RentalTier.STARTER,
        RentalTier.PRO,
        RentalTier.PORTFOLIO,
        RentalTier.ELITE,
    ]


def test_catalog_by_id_lookup() -> None:
    assert DEFAULT_CATALOG.by_id(RentalTier.PRO) is PRO
    assert DEFAULT_CATALOG.by_id(RentalTier.ELITE) is ELITE


def test_catalog_by_id_rejects_unknown() -> None:
    cat = TierCatalog(tiers=(TRIAL, STARTER))
    with pytest.raises(KeyError):
        cat.by_id(RentalTier.ELITE)


# ---------------------------------------------------------------------------
# Pricing ladder
# ---------------------------------------------------------------------------


def test_trial_is_free_and_paper_only() -> None:
    assert TRIAL.monthly_usd == 0.0
    assert TRIAL.quarterly_usd == 0.0
    assert TRIAL.annual_usd == 0.0
    assert TRIAL.trial_days == 7
    assert TRIAL.max_equity_managed_usd == 0


def test_pricing_ladder_is_monotonic_monthly() -> None:
    ladder = [STARTER, PRO, PORTFOLIO, ELITE]
    prices = [t.monthly_usd for t in ladder]
    assert prices == sorted(prices), "monthly price must not regress as tier climbs"


def test_pricing_ladder_is_monotonic_annual() -> None:
    ladder = [STARTER, PRO, PORTFOLIO, ELITE]
    prices = [t.annual_usd for t in ladder]
    assert prices == sorted(prices)


def test_quarterly_discount_vs_monthly() -> None:
    # quarterly should be cheaper than 3 months rolled up
    for t in (STARTER, PRO, PORTFOLIO, ELITE):
        assert t.quarterly_usd < 3.0 * t.monthly_usd, f"{t.id} quarterly no-discount"


def test_annual_discount_vs_monthly() -> None:
    for t in (STARTER, PRO, PORTFOLIO, ELITE):
        assert t.annual_usd < 12.0 * t.monthly_usd, f"{t.id} annual no-discount"


def test_tier_competitor_range_match() -> None:
    # STARTER anchors within the $25-90 SaaS range
    assert 25.0 <= STARTER.monthly_usd <= 99.0
    # PRO above cheap competitors (brain premium)
    assert PRO.monthly_usd > 90.0


# ---------------------------------------------------------------------------
# SKU access
# ---------------------------------------------------------------------------


def test_elite_grants_every_sku() -> None:
    assert ELITE.bot_skus == frozenset(BotSku)
    assert BotSku.MNQ_APEX in ELITE.bot_skus


def test_mnq_apex_is_elite_only() -> None:
    for tier in (TRIAL, STARTER, PRO, PORTFOLIO):
        assert BotSku.MNQ_APEX not in tier.bot_skus


def test_portfolio_tier_covers_layer2_3_4() -> None:
    expected = {
        BotSku.BTC_SEED,
        BotSku.ETH_PERP,
        BotSku.SOL_PERP,
        BotSku.STAKING_SWEEP,
    }
    assert expected.issubset(PORTFOLIO.bot_skus)


def test_starter_is_btc_only() -> None:
    assert STARTER.bot_skus == frozenset({BotSku.BTC_SEED})


# ---------------------------------------------------------------------------
# public_price_list
# ---------------------------------------------------------------------------


def test_public_price_list_is_json_safe() -> None:
    import json

    price_list = DEFAULT_CATALOG.public_price_list()
    # Round-trip through json ensures only JSON primitives
    dumped = json.dumps(price_list)
    loaded = json.loads(dumped)
    assert isinstance(loaded, list)
    assert len(loaded) == 5


def test_public_price_list_sorted_bot_skus() -> None:
    price_list = DEFAULT_CATALOG.public_price_list()
    for row in price_list:
        skus = row["bot_skus"]
        assert skus == sorted(skus), f"bot_skus not sorted: {skus}"


def test_public_price_list_preserves_all_fields() -> None:
    row = DEFAULT_CATALOG.public_price_list()[0]  # TRIAL
    assert set(row.keys()) == {
        "id",
        "display_name",
        "monthly_usd",
        "quarterly_usd",
        "annual_usd",
        "bot_skus",
        "recommended_min_capital_usd",
        "max_concurrent_positions",
        "max_equity_managed_usd",
        "includes_custom_tweaks",
        "includes_priority_retrain",
        "trial_days",
        "description",
    }


# ---------------------------------------------------------------------------
# price_for helper
# ---------------------------------------------------------------------------


def test_price_for_accepts_tier_and_string_cycle() -> None:
    assert price_for(PRO, "monthly") == PRO.monthly_usd
    assert price_for(PRO, "quarterly") == PRO.quarterly_usd
    assert price_for(PRO, "annual") == PRO.annual_usd


def test_price_for_accepts_rental_tier_enum() -> None:
    assert price_for(RentalTier.STARTER, "monthly") == STARTER.monthly_usd


def test_price_for_rejects_unknown_cycle() -> None:
    with pytest.raises(ValueError, match="unknown billing cycle"):
        price_for(PRO, "biennial")
