"""
JARVIS v3 // claude_layer.stakes
================================
Layer 3 -- stakes classification.

Given an escalated decision, classify its stakes (LOW / MEDIUM / HIGH /
CRITICAL). Stakes determines which Claude tier handles it.

Mapping (operator-tunable):

  LOW      -> Haiku  (cheap consult)
  MEDIUM   -> Sonnet (default)
  HIGH     -> Sonnet + Opus-skeptic (single Opus call on the skeptic role only)
  CRITICAL -> full Opus quartet (all four personas on Opus)

Stakes derivation is deterministic from features JARVIS already has:

  * R at risk
  * regime severity
  * action type (KILL_SWITCH vs ORDER_PLACE)
  * portfolio breach
  * operator override velocity
  * tier (paper / live)

Pure stdlib + pydantic.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.model_policy import ModelTier


class Stakes(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class StakesInputs(BaseModel):
    """Inputs used to classify stakes. All optional with safe defaults."""

    model_config = ConfigDict(frozen=True)

    regime: str = "NEUTRAL"
    action: str = "ORDER_PLACE"
    r_at_risk: float = Field(ge=0.0, default=0.0)
    is_live: bool = False
    portfolio_breach: bool = False
    doctrine_net_bias: float = Field(ge=-1.0, le=1.0, default=0.0)
    operator_overrides_24h: int = Field(ge=0, default=0)
    capital_exposed_pct: float = Field(ge=0.0, le=1.0, default=0.0)


class StakesVerdict(BaseModel):
    """Classified stakes + model-tier pick."""

    model_config = ConfigDict(frozen=True)

    stakes: Stakes
    model_tier: ModelTier
    skeptic_tier: ModelTier  # the persona that MIGHT go to a higher tier
    reasons: list[str]


# Stakes -> baseline model tier (everyone except skeptic)
_STAKES_TO_TIER: dict[Stakes, ModelTier] = {
    Stakes.LOW: ModelTier.HAIKU,
    Stakes.MEDIUM: ModelTier.SONNET,
    Stakes.HIGH: ModelTier.SONNET,
    Stakes.CRITICAL: ModelTier.OPUS,
}

# Skeptic is the one persona that benefits most from extra reasoning.
# Upgrade the skeptic tier at HIGH (Sonnet -> Opus) while keeping others
# on Sonnet; at CRITICAL everyone goes Opus.
_STAKES_TO_SKEPTIC_TIER: dict[Stakes, ModelTier] = {
    Stakes.LOW: ModelTier.HAIKU,
    Stakes.MEDIUM: ModelTier.SONNET,
    Stakes.HIGH: ModelTier.OPUS,
    Stakes.CRITICAL: ModelTier.OPUS,
}


# Actions that pin stakes regardless of other signals.
_CRITICAL_ACTIONS = frozenset(
    {
        "KILL_SWITCH_TRIP",
        "KILL_SWITCH_RESET",
        "GATE_OVERRIDE",
        "STRATEGY_DEPLOY",
        "CAPITAL_ALLOCATE",
    }
)
_HIGH_ACTIONS = frozenset(
    {
        "STRATEGY_RETIRE",
        "PARAMETER_CHANGE",
        "PROTOCOL_EXPOSURE",
        "AUTOPILOT_RESUME",
    }
)


def classify_stakes(inp: StakesInputs) -> StakesVerdict:
    """Pure classifier: features -> Stakes + tier selections."""
    reasons: list[str] = []
    stakes = Stakes.MEDIUM  # default

    # Action pins
    if inp.action in _CRITICAL_ACTIONS:
        stakes = Stakes.CRITICAL
        reasons.append(f"{inp.action} -> CRITICAL")
    elif inp.action in _HIGH_ACTIONS:
        stakes = Stakes.HIGH
        reasons.append(f"{inp.action} -> HIGH")

    # Live is always at least HIGH
    if inp.is_live and stakes.value in {"LOW", "MEDIUM"}:
        stakes = Stakes.HIGH
        reasons.append("live mode -> at least HIGH")

    # Crisis regime is at least HIGH
    if inp.regime.upper() == "CRISIS" and stakes.value == "MEDIUM":
        stakes = Stakes.HIGH
        reasons.append("CRISIS regime -> HIGH")

    # R at risk
    if inp.r_at_risk >= 3.0:
        stakes = max(stakes, Stakes.CRITICAL, key=_rank)
        reasons.append(f"R-at-risk {inp.r_at_risk:.1f} >= 3 -> CRITICAL")
    elif inp.r_at_risk >= 1.5:
        stakes = max(stakes, Stakes.HIGH, key=_rank)
        reasons.append(f"R-at-risk {inp.r_at_risk:.1f} >= 1.5 -> HIGH")

    # Portfolio breach
    if inp.portfolio_breach:
        stakes = max(stakes, Stakes.HIGH, key=_rank)
        reasons.append("portfolio breach -> HIGH")

    # Doctrine conflict
    if inp.doctrine_net_bias <= -0.50:
        stakes = max(stakes, Stakes.HIGH, key=_rank)
        reasons.append(f"doctrine bias {inp.doctrine_net_bias:+.2f} heavily negative -> HIGH")

    # Operator override velocity
    if inp.operator_overrides_24h >= 5:
        stakes = max(stakes, Stakes.HIGH, key=_rank)
        reasons.append(f"operator overrode {inp.operator_overrides_24h}x in 24h -> HIGH")

    # Capital exposed
    if inp.capital_exposed_pct >= 0.60:
        stakes = max(stakes, Stakes.CRITICAL, key=_rank)
        reasons.append(f"{inp.capital_exposed_pct:.0%} of capital exposed -> CRITICAL")

    # LOW only if truly nothing elevates
    if not reasons and not inp.is_live and inp.r_at_risk < 0.5:
        stakes = Stakes.LOW
        reasons.append("no elevation signals, paper-mode -> LOW")

    tier = _STAKES_TO_TIER[stakes]
    skeptic_tier = _STAKES_TO_SKEPTIC_TIER[stakes]
    return StakesVerdict(
        stakes=stakes,
        model_tier=tier,
        skeptic_tier=skeptic_tier,
        reasons=reasons or ["default MEDIUM"],
    )


_RANK: dict[Stakes, int] = {
    Stakes.LOW: 0,
    Stakes.MEDIUM: 1,
    Stakes.HIGH: 2,
    Stakes.CRITICAL: 3,
}


def _rank(s: Stakes) -> int:
    return _RANK[s]
