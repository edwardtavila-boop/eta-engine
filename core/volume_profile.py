"""Volume profile / auction market analytics (Tier-1 #3, part 2, 2026-04-27).

Computes price-bucketed volume profile + the canonical auction market
levels that Wyckoff/Auction Theory bots care about:

  * POC  (Point of Control)         -- price bucket with most volume
  * VAH  (Value Area High)          -- top of the 70% value area
  * VAL  (Value Area Low)           -- bottom of the 70% value area
  * HVN  (High Volume Nodes)        -- buckets above N% of POC volume
  * LVN  (Low Volume Nodes)         -- buckets below M% of POC volume

Bots use POC/VAH/VAL as gravitational levels: trades INTO the value
area get a confluence bonus; trades AGAINST a fresh LVN get a penalty.

Inputs are pre-bucketed (price_bucket -> volume) so the caller chooses
the bucket size (typically 1 tick or 0.1% of price for crypto).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VolumeProfile:
    poc: float  # price of the most-traded bucket
    vah: float  # value area high (70% containment)
    val: float  # value area low
    total_volume: float
    poc_volume: float
    hvn_levels: list[float]  # high-volume nodes
    lvn_levels: list[float]  # low-volume nodes


def compute_profile(
    buckets: dict[float, float],
    *,
    value_area_pct: float = 0.70,
    hvn_threshold_pct: float = 0.80,  # >= 80% of POC volume
    lvn_threshold_pct: float = 0.20,  # <= 20% of POC volume
) -> VolumeProfile:
    """Compute the canonical levels from a price->volume mapping.

    ``buckets`` is ``{price: volume}``. Caller pre-bucketizes raw fills
    (typically by rounding to tick size).
    """
    if not buckets:
        return VolumeProfile(
            poc=0.0,
            vah=0.0,
            val=0.0,
            total_volume=0.0,
            poc_volume=0.0,
            hvn_levels=[],
            lvn_levels=[],
        )

    sorted_prices = sorted(buckets.keys())
    total = sum(buckets.values())
    poc_price = max(buckets, key=lambda k: buckets[k])
    poc_volume = buckets[poc_price]

    # Value area: expand from POC outward until we cover value_area_pct
    target_volume = total * value_area_pct
    above_idx = sorted_prices.index(poc_price)
    below_idx = above_idx
    accumulated = poc_volume

    while accumulated < target_volume and (above_idx < len(sorted_prices) - 1 or below_idx > 0):
        # Compare adding next-above vs next-below; take the larger
        next_above = buckets[sorted_prices[above_idx + 1]] if above_idx < len(sorted_prices) - 1 else -1
        next_below = buckets[sorted_prices[below_idx - 1]] if below_idx > 0 else -1
        if next_above >= next_below:
            above_idx += 1
            accumulated += next_above
        else:
            below_idx -= 1
            accumulated += next_below

    vah = sorted_prices[above_idx]
    val = sorted_prices[below_idx]

    hvn_threshold = poc_volume * hvn_threshold_pct
    lvn_threshold = poc_volume * lvn_threshold_pct
    hvn_levels = sorted([p for p, v in buckets.items() if v >= hvn_threshold])
    lvn_levels = sorted([p for p, v in buckets.items() if v <= lvn_threshold])

    return VolumeProfile(
        poc=round(poc_price, 6),
        vah=round(vah, 6),
        val=round(val, 6),
        total_volume=round(total, 4),
        poc_volume=round(poc_volume, 4),
        hvn_levels=[round(p, 6) for p in hvn_levels],
        lvn_levels=[round(p, 6) for p in lvn_levels],
    )


def position_relative_to_value_area(
    price: float,
    profile: VolumeProfile,
) -> str:
    """Categorize a price relative to the value area.

    Returns "above_vah", "in_value", "below_val", or "no_profile".
    """
    if profile.total_volume == 0:
        return "no_profile"
    if price > profile.vah:
        return "above_vah"
    if price < profile.val:
        return "below_val"
    return "in_value"


def is_near_lvn(price: float, profile: VolumeProfile, *, tolerance_pct: float = 0.001) -> bool:
    """True if ``price`` is within ``tolerance_pct`` of any LVN.

    LVNs are price levels where prior trading was thin -- price tends
    to slice through them quickly (no resistance) AND/OR fail to find
    interest there (rejection). Either way, a trade hovering near an
    LVN is at higher risk.
    """
    return any(abs(price - lvn) / max(price, 1e-9) <= tolerance_pct for lvn in profile.lvn_levels)
