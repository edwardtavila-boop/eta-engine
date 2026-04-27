"""
EVOLUTIONARY TRADING ALGO  //  rental.tiers
===============================
SKU + pricing catalog for APEX bot rentals.

Pricing anchors (2026 SaaS comparables):
  TradeSanta ($25-90/mo), 3Commas ($15-99/mo), Cryptohopper (similar).
Our differentiator is the proprietary brain -- Apex Governor + custom PPO
reward + regime logic -- so tiers land ABOVE the cheap end but with a
7-day paper trial to de-risk signup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BotSku(StrEnum):
    """Which bot(s) a tier grants access to."""

    MNQ_APEX = "MNQ_APEX"  # Layer 1 -- futures
    BTC_SEED = "BTC_SEED"  # Layer 2 -- grid + overlay
    ETH_PERP = "ETH_PERP"  # Layer 3 -- eth perps
    SOL_PERP = "SOL_PERP"  # Layer 3 -- sol perps
    STAKING_SWEEP = "STAKING_SWEEP"  # Layer 4 -- passive accumulator


class RentalTier(StrEnum):
    """Named product tiers."""

    TRIAL = "TRIAL"
    STARTER = "STARTER"
    PRO = "PRO"
    PORTFOLIO = "PORTFOLIO"
    ELITE = "ELITE"


@dataclass(frozen=True)
class Tier:
    """One SKU row in the public price list."""

    id: RentalTier
    display_name: str
    monthly_usd: float
    quarterly_usd: float
    annual_usd: float
    bot_skus: frozenset[BotSku]
    recommended_min_capital_usd: int
    max_concurrent_positions: int
    max_equity_managed_usd: int | None
    includes_custom_tweaks: bool = False
    includes_priority_retrain: bool = False
    description: str = ""
    trial_days: int = 0


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


TRIAL = Tier(
    id=RentalTier.TRIAL,
    display_name="Apex 7-Day Trial (paper only)",
    monthly_usd=0.0,
    quarterly_usd=0.0,
    annual_usd=0.0,
    bot_skus=frozenset({BotSku.BTC_SEED}),
    recommended_min_capital_usd=0,
    max_concurrent_positions=1,
    max_equity_managed_usd=0,
    description=(
        "Paper-trade BTC Seed for 7 days. No live orders, full dashboard access, "
        "auto-expires back to guest state at day 8."
    ),
    trial_days=7,
)

STARTER = Tier(
    id=RentalTier.STARTER,
    display_name="Apex Starter",
    monthly_usd=49.0,
    quarterly_usd=129.0,  # ~$43/mo  (12% off monthly)
    annual_usd=469.0,  # ~$39/mo  (20% off monthly)
    bot_skus=frozenset({BotSku.BTC_SEED}),
    recommended_min_capital_usd=2_000,
    max_concurrent_positions=3,
    max_equity_managed_usd=25_000,
    description="Single-bot rental: BTC grid + directional overlay on your Bybit/OKX account.",
)

PRO = Tier(
    id=RentalTier.PRO,
    display_name="Apex Pro",
    monthly_usd=99.0,
    quarterly_usd=259.0,
    annual_usd=949.0,
    bot_skus=frozenset({BotSku.BTC_SEED, BotSku.ETH_PERP}),
    recommended_min_capital_usd=5_000,
    max_concurrent_positions=6,
    max_equity_managed_usd=75_000,
    description="BTC Seed + ETH Perps. Shared Apex Governor, correlation guard, vol-scaling.",
)

PORTFOLIO = Tier(
    id=RentalTier.PORTFOLIO,
    display_name="Apex Portfolio",
    monthly_usd=179.0,
    quarterly_usd=479.0,
    annual_usd=1_699.0,
    bot_skus=frozenset({BotSku.BTC_SEED, BotSku.ETH_PERP, BotSku.SOL_PERP, BotSku.STAKING_SWEEP}),
    recommended_min_capital_usd=15_000,
    max_concurrent_positions=10,
    max_equity_managed_usd=250_000,
    includes_custom_tweaks=True,
    description=(
        "Full Layer 2 + Layer 3 + Layer 4. Custom risk tweaks unlocked. Profit sweeps to staking automatically enabled."
    ),
)

ELITE = Tier(
    id=RentalTier.ELITE,
    display_name="Apex Elite",
    monthly_usd=299.0,
    quarterly_usd=799.0,
    annual_usd=2_799.0,
    bot_skus=frozenset(BotSku),
    recommended_min_capital_usd=50_000,
    max_concurrent_positions=20,
    max_equity_managed_usd=None,  # uncapped
    includes_custom_tweaks=True,
    includes_priority_retrain=True,
    description=(
        "Every bot, including MNQ Apex futures. Priority retraining slot, "
        "private Discord channel, monthly tuning review."
    ),
)


@dataclass(frozen=True)
class TierCatalog:
    """Immutable catalog of available tiers."""

    tiers: tuple[Tier, ...] = field(
        default_factory=lambda: (TRIAL, STARTER, PRO, PORTFOLIO, ELITE),
    )

    def by_id(self, tier_id: RentalTier) -> Tier:
        for t in self.tiers:
            if t.id == tier_id:
                return t
        raise KeyError(f"unknown tier {tier_id!r}")

    def public_price_list(self) -> list[dict[str, object]]:
        """Serializable form for the website / Stripe checkout bootstrap."""
        return [
            {
                "id": t.id.value,
                "display_name": t.display_name,
                "monthly_usd": t.monthly_usd,
                "quarterly_usd": t.quarterly_usd,
                "annual_usd": t.annual_usd,
                "bot_skus": sorted(s.value for s in t.bot_skus),
                "recommended_min_capital_usd": t.recommended_min_capital_usd,
                "max_concurrent_positions": t.max_concurrent_positions,
                "max_equity_managed_usd": t.max_equity_managed_usd,
                "includes_custom_tweaks": t.includes_custom_tweaks,
                "includes_priority_retrain": t.includes_priority_retrain,
                "trial_days": t.trial_days,
                "description": t.description,
            }
            for t in self.tiers
        ]


DEFAULT_CATALOG = TierCatalog()


def price_for(tier: Tier | RentalTier, cycle: str) -> float:
    """Price lookup that accepts ``"monthly" | "quarterly" | "annual"``.

    Non-standard cycles raise ValueError rather than silently falling back.
    """
    resolved = tier if isinstance(tier, Tier) else DEFAULT_CATALOG.by_id(tier)
    match cycle:
        case "monthly":
            return resolved.monthly_usd
        case "quarterly":
            return resolved.quarterly_usd
        case "annual":
            return resolved.annual_usd
        case _:
            raise ValueError(
                f"unknown billing cycle {cycle!r}; expected monthly/quarterly/annual",
            )
