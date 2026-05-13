"""Market impact estimator (Tier-2 #14, 2026-04-27).

Almgren-Chriss linear-impact approximation: when you scale your size
up, you start moving the book. Even at small retail size this matters
on thin instruments (SOL/XRP CME futures, overnight MNQ/NQ).

Formula (simplified):
    impact_bps ≈ k * (qty / avg_daily_volume) ^ alpha

Where:
  * k     -- impact coefficient (~10 bps for liquid futures)
  * alpha -- typically 0.5 (square-root law)

Bots should consult this BEFORE submitting size to estimate the cost
penalty:

    from eta_engine.core.market_impact import estimate_impact_bps

    bps = estimate_impact_bps(
        symbol="MBT",
        qty=2,
        avg_daily_volume=10_000,
    )
    if bps > 5.0:
        # Consider TWAP'ing the order rather than market-ing it
        ...
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImpactProfile:
    """Per-symbol calibrated impact parameters."""

    symbol: str
    k_bps: float  # impact coefficient
    alpha: float  # power-law exponent (typical 0.5)
    avg_daily_volume: float
    notes: str = ""


# Operator-tuned defaults. Refresh quarterly from realized fills.
DEFAULT_PROFILES: dict[str, ImpactProfile] = {
    "MNQ": ImpactProfile("MNQ", k_bps=8.0, alpha=0.5, avg_daily_volume=1_500_000),
    "NQ": ImpactProfile("NQ", k_bps=12.0, alpha=0.5, avg_daily_volume=350_000),
    "MBT": ImpactProfile(
        "MBT",
        k_bps=20.0,
        alpha=0.55,
        avg_daily_volume=15_000,
        notes="thin compared to BTC futures; impact more visible",
    ),
    "MET": ImpactProfile("MET", k_bps=18.0, alpha=0.55, avg_daily_volume=20_000),
    "BTC": ImpactProfile("BTC", k_bps=10.0, alpha=0.5, avg_daily_volume=11_000),
    "ETH": ImpactProfile("ETH", k_bps=14.0, alpha=0.5, avg_daily_volume=80_000),
    "SOL": ImpactProfile(
        "SOL", k_bps=25.0, alpha=0.55, avg_daily_volume=5_000, notes="newer CME contract; thin book at fringes"
    ),
    "XRP": ImpactProfile(
        "XRP", k_bps=30.0, alpha=0.6, avg_daily_volume=3_000, notes="newest CME contract; widest spreads"
    ),
}


def estimate_impact_bps(
    *,
    symbol: str,
    qty: float,
    profile: ImpactProfile | None = None,
    avg_daily_volume: float | None = None,
) -> float:
    """Estimate market impact in BASIS POINTS for a given order size.

    Caller can override the profile (e.g. live-tuned) or just supply
    ``avg_daily_volume`` to use a known-symbol default.
    """
    if profile is None:
        profile = DEFAULT_PROFILES.get(symbol.upper())
    if profile is None:
        return 0.0  # no profile -> no estimate -> caller should consult traditional spread

    adv = avg_daily_volume if avg_daily_volume is not None else profile.avg_daily_volume
    if adv <= 0 or qty <= 0:
        return 0.0
    fraction = qty / adv
    impact = profile.k_bps * (fraction**profile.alpha)
    return round(impact, 3)


def is_size_too_aggressive(
    *,
    symbol: str,
    qty: float,
    threshold_bps: float = 10.0,
) -> bool:
    """Convenience: True when estimated impact exceeds threshold.

    Default 10 bps: above this, the trade probably should be split
    (TWAP / iceberg) rather than fired as a single market order.
    """
    return estimate_impact_bps(symbol=symbol, qty=qty) > threshold_bps
