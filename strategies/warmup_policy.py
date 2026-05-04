"""
EVOLUTIONARY TRADING ALGO  //  strategies.warmup_policy
=======================================================
First-30-days half-size policy for newly-promoted bots.

Devils-advocate calibration on 2026-04-27: "calibrated probability
any single pick has real edge: ~25%. MNQ ORB and ETH sweep_reclaim
have nominal walk-forward evidence; the rest are assertions." Mitigation
explicitly recommended: *half size, per-bot daily loss cap, weekly
out-of-sample Sharpe gate, and crypto_seed disabled until regime
gate lands.*

This module encodes the half-size half: any bot whose registry
assignment carries an ``extras["warmup_policy"]`` entry runs at a
reduced risk multiplier for the first N trading days after its
promotion date. After the warm-up window the multiplier reverts
to 1.0 automatically (no operator action needed).

Registry shape::

    extras={
        "warmup_policy": {
            "promoted_on": "2026-04-27",
            "warmup_days": 30,
            "risk_multiplier_during_warmup": 0.5,
        },
    }

All three keys are required when ``warmup_policy`` is present;
malformed policies are treated as a no-op (multiplier=1.0) so a
typo can't accidentally amplify risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.strategies.per_bot_registry import StrategyAssignment


@dataclass(frozen=True)
class WarmupPolicy:
    """Parsed shape of ``extras["warmup_policy"]``."""

    promoted_on: date
    warmup_days: int
    risk_multiplier_during_warmup: float

    @classmethod
    def from_extras(cls, extras: dict[str, object] | None) -> WarmupPolicy | None:
        """Best-effort parse. Returns ``None`` on missing or malformed."""
        if not extras:
            return None
        raw = extras.get("warmup_policy")
        if not isinstance(raw, dict):
            return None
        try:
            promoted = date.fromisoformat(str(raw["promoted_on"]))
            days = int(raw["warmup_days"])
            mult = float(raw["risk_multiplier_during_warmup"])
        except (KeyError, TypeError, ValueError):
            return None
        if days < 0:
            return None
        if not 0.0 < mult <= 2.0:
            # 0 would silently mute; >2 would amplify — neither is
            # a safe accident-class default. Refuse to parse.
            return None
        return cls(
            promoted_on=promoted,
            warmup_days=days,
            risk_multiplier_during_warmup=mult,
        )


def _today_utc() -> date:
    return datetime.now(tz=UTC).date()


def warmup_risk_multiplier(
    assignment: StrategyAssignment | None,
    *,
    today: date | None = None,
) -> float:
    """Resolve the warm-up multiplier for ``assignment`` on ``today``.

    Returns 1.0 (no shrinkage) when:
      * ``assignment`` is None.
      * ``extras["warmup_policy"]`` is missing or malformed.
      * The warm-up window has already elapsed.
      * ``promoted_on`` is in the future.

    Returns the configured ``risk_multiplier_during_warmup``
    iff today is within ``[promoted_on, promoted_on + warmup_days)``.
    """
    if assignment is None:
        return 1.0
    policy = WarmupPolicy.from_extras(assignment.extras)
    if policy is None:
        return 1.0
    today = today or _today_utc()
    if today < policy.promoted_on:
        return 1.0
    days_in = (today - policy.promoted_on).days
    if days_in >= policy.warmup_days:
        return 1.0
    return policy.risk_multiplier_during_warmup


def warmup_risk_multiplier_for_bot(
    bot_id: str,
    *,
    today: date | None = None,
) -> float:
    """Convenience: ``warmup_risk_multiplier`` keyed by ``bot_id``."""
    from eta_engine.strategies.per_bot_registry import get_for_bot

    return warmup_risk_multiplier(get_for_bot(bot_id), today=today)
