"""
EVOLUTIONARY TRADING ALGO  //  sweep_engine
===============================
Profit funnel sweep logic.
Excess capital gets split: stake, reinvest, reserve.
Automatic. Disciplined. No touching the profits.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class SweepSplit(BaseModel):
    """How excess profits get allocated."""

    cold_stake_pct: float = Field(
        default=60.0,
        ge=0,
        le=100,
        description="% to cold staking / yield",
    )
    reinvest_pct: float = Field(
        default=30.0,
        ge=0,
        le=100,
        description="% back into trading capital",
    )
    reserve_pct: float = Field(
        default=10.0,
        ge=0,
        le=100,
        description="% to emergency reserve",
    )

    def model_post_init(self, __context: Any) -> None:
        total = self.cold_stake_pct + self.reinvest_pct + self.reserve_pct
        if abs(total - 100.0) > 0.01:
            raise ValueError(f"Split must sum to 100%, got {total:.2f}%")


class SweepConfig(BaseModel):
    """Configuration for a single bot's sweep behavior."""

    bot_name: str
    baseline_usd: float = Field(gt=0, description="Target equity baseline")
    trigger_multiplier: float = Field(
        default=1.10,
        gt=1.0,
        description="Sweep when equity >= baseline * multiplier",
    )
    split: SweepSplit = Field(default_factory=SweepSplit)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


class SweepResult(BaseModel):
    """Outcome of a sweep check."""

    excess_usd: float = Field(ge=0, description="Amount above trigger threshold")
    to_stake: float = Field(ge=0)
    to_reinvest: float = Field(ge=0)
    to_reserve: float = Field(ge=0)
    action_required: bool = Field(description="True if excess > 0 and sweep should execute")
    bot_name: str = ""
    trigger_price: float = Field(default=0, description="Equity level that triggered sweep")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def check_sweep(
    current_equity: float,
    config: SweepConfig,
) -> SweepResult:
    """Check if a bot's equity exceeds the sweep trigger.

    Sweep fires when: current_equity >= baseline * trigger_multiplier.
    Excess = current_equity - baseline (keep baseline intact).

    Returns SweepResult with per-bucket allocations.
    """
    if current_equity <= 0:
        raise ValueError(f"Equity must be positive, got {current_equity}")

    trigger = config.baseline_usd * config.trigger_multiplier

    if current_equity < trigger:
        return SweepResult(
            excess_usd=0.0,
            to_stake=0.0,
            to_reinvest=0.0,
            to_reserve=0.0,
            action_required=False,
            bot_name=config.bot_name,
            trigger_price=trigger,
        )

    excess = round(current_equity - config.baseline_usd, 2)

    return SweepResult(
        excess_usd=excess,
        to_stake=round(excess * config.split.cold_stake_pct / 100.0, 2),
        to_reinvest=round(excess * config.split.reinvest_pct / 100.0, 2),
        to_reserve=round(excess * config.split.reserve_pct / 100.0, 2),
        action_required=True,
        bot_name=config.bot_name,
        trigger_price=trigger,
    )


# ---------------------------------------------------------------------------
# Execution stub
# ---------------------------------------------------------------------------


async def execute_sweep(
    result: SweepResult,
    destination: str | None = None,
) -> dict[str, Any]:
    """Execute the sweep by moving funds to designated accounts.

    TODO: integrate with exchange withdrawal API
    TODO: integrate with staking platform deposit API
    TODO: log sweep to event journal
    TODO: send notification (Discord / Telegram)

    Args:
        result: SweepResult from check_sweep.
        destination: Target wallet / account identifier.

    Returns:
        Execution receipt with transaction IDs.
    """
    if not result.action_required:
        return {"status": "skipped", "reason": "no_excess"}

    # Placeholder: real implementation calls exchange + staking APIs
    receipt: dict[str, Any] = {
        "status": "pending",
        "bot_name": result.bot_name,
        "excess_usd": result.excess_usd,
        "allocations": {
            "cold_stake": result.to_stake,
            "reinvest": result.to_reinvest,
            "reserve": result.to_reserve,
        },
        "destination": destination,
        "tx_ids": {
            "stake_tx": None,  # TODO: fill after API call
            "reinvest_tx": None,
            "reserve_tx": None,
        },
    }
    return receipt
