"""
EVOLUTIONARY TRADING ALGO  //  staking.allocator
====================================
Auto-allocation engine for the Multiplier layer.
Split excess capital across yield sources. Rebalance on drift.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from eta_engine.funnel.transfer import TransferRequest

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class AllocationConfig(BaseModel):
    """Target allocation percentages across yield sources."""

    eth_pct: float = Field(default=40.0, ge=0, le=100)
    sol_pct: float = Field(default=30.0, ge=0, le=100)
    xrp_pct: float = Field(default=15.0, ge=0, le=100)
    stable_pct: float = Field(default=15.0, ge=0, le=100)
    diversification_cap: float = Field(
        default=40.0,
        ge=0,
        le=100,
        description="Max allocation to any single asset class",
    )

    @model_validator(mode="after")
    def _check_totals(self) -> AllocationConfig:
        total = self.eth_pct + self.sol_pct + self.xrp_pct + self.stable_pct
        if abs(total - 100.0) > 0.01:
            raise ValueError(f"Allocations must sum to 100%, got {total:.2f}%")
        return self

    @model_validator(mode="after")
    def _check_cap(self) -> AllocationConfig:
        for name in ("eth_pct", "sol_pct", "xrp_pct", "stable_pct"):
            val = getattr(self, name)
            if val > self.diversification_cap:
                raise ValueError(f"{name}={val}% exceeds diversification cap {self.diversification_cap}%")
        return self


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------

_ASSET_KEYS = ("eth", "sol", "xrp", "stable")


def allocate(total_usd: float, config: AllocationConfig | None = None) -> dict[str, float]:
    """Compute per-asset USD allocation from total excess capital.

    Returns:
        {"eth": 4000.0, "sol": 3000.0, "xrp": 1500.0, "stable": 1500.0}
    """
    if total_usd <= 0:
        return {k: 0.0 for k in _ASSET_KEYS}
    if config is None:
        config = AllocationConfig()

    pcts = {
        "eth": config.eth_pct,
        "sol": config.sol_pct,
        "xrp": config.xrp_pct,
        "stable": config.stable_pct,
    }
    return {k: round(total_usd * v / 100.0, 2) for k, v in pcts.items()}


# ---------------------------------------------------------------------------
# Rebalancing
# ---------------------------------------------------------------------------


async def rebalance(
    current_balances: dict[str, float],
    target_config: AllocationConfig | None = None,
) -> list[TransferRequest]:
    """Generate transfer requests to rebalance from current to target allocation.

    Only generates transfers for drift > 2% of portfolio to avoid churn.

    Returns:
        List of TransferRequests — from over-allocated to under-allocated.
    """
    if target_config is None:
        target_config = AllocationConfig()

    total = sum(current_balances.get(k, 0.0) for k in _ASSET_KEYS)
    if total <= 0:
        return []

    targets = allocate(total, target_config)
    diffs: dict[str, float] = {}
    for k in _ASSET_KEYS:
        diffs[k] = targets[k] - current_balances.get(k, 0.0)

    # Sort: positive diff = needs capital, negative = has excess
    over = {k: -v for k, v in diffs.items() if v < 0}
    under = {k: v for k, v in diffs.items() if v > 0}

    drift_threshold = total * 0.02  # 2% minimum drift to act
    transfers: list[TransferRequest] = []

    for src, src_excess in sorted(over.items(), key=lambda x: -x[1]):
        if src_excess < drift_threshold:
            continue
        for dst, dst_need in sorted(under.items(), key=lambda x: -x[1]):
            if dst_need < drift_threshold:
                continue
            move = min(src_excess, dst_need)
            if move < drift_threshold:
                continue
            transfers.append(
                TransferRequest(
                    from_bot=f"staking_{src}",
                    to_bot=f"staking_{dst}",
                    amount_usd=round(move, 2),
                    reason=f"Rebalance {src}->{dst}",
                )
            )
            src_excess -= move
            under[dst] -= move
            if src_excess < drift_threshold:
                break

    return transfers
