"""
JARVIS v3 // philosophy
=======================
The Evolutionary Trading Algo core doctrine.

Codifies the operator's risk / behavior / epistemic doctrine into a
machine-consumable form so JARVIS reads it the same way every time.

Kaizen (continuous improvement) and the seven operating tenets below are
NOT decorative. Every tenet maps to:
  1. a *bias* -- a number between -1 and +1 that nudges a verdict /
     stress component / model tier in the direction the tenet demands
  2. a *pre-condition* -- a callable the caller can run to check if a
     proposed action honors the tenet
  3. a *violation_alert* -- a stable code the supervisor emits when a
     tenet is breached

The point: JARVIS does not "decide" based on the doctrine; JARVIS
UPHOLDS the doctrine. The doctrine is the constitution.

Pure / frozen / deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Tenet(StrEnum):
    """The seven Evolutionary Trading Algo tenets. Ordered by priority."""

    CAPITAL_FIRST = "CAPITAL_FIRST"  # never blow up
    EDGE_IS_FRAGILE = "EDGE_IS_FRAGILE"  # assume it decays
    PROCESS_OVER_OUTCOME = "PROCESS_OVER_OUTCOME"  # grade the trade, not the P&L
    KAIZEN = "KAIZEN"  # 1% better every cycle
    ADVERSARIAL_HONESTY = "ADVERSARIAL_HONESTY"  # red team your own data
    OBSERVABILITY = "OBSERVABILITY"  # if you didn't log it, it didn't happen
    NEVER_ON_AUTOPILOT = "NEVER_ON_AUTOPILOT"  # human in the loop at every tier


@dataclass(frozen=True)
class TenetSpec:
    """One tenet's contract with the system."""

    tenet: Tenet
    statement: str
    bias: float  # [-1, +1]: sign of the nudge JARVIS applies
    violation_code: str  # stable alert code on breach
    applies_to: tuple[str, ...]  # which subsystem categories it bites on


# The canonical, frozen doctrine. Do not mutate.
DOCTRINE: dict[Tenet, TenetSpec] = {
    Tenet.CAPITAL_FIRST: TenetSpec(
        tenet=Tenet.CAPITAL_FIRST,
        statement=(
            "Preserve capital first, grow it second. A 50% drawdown requires "
            "a 100% gain to recover -- the math hates you when you lose big."
        ),
        bias=-0.40,
        violation_code="capital_first_breach",
        applies_to=("bot.*", "framework.firm_engine"),
    ),
    Tenet.EDGE_IS_FRAGILE: TenetSpec(
        tenet=Tenet.EDGE_IS_FRAGILE,
        statement=(
            "Every edge decays. Sample size, regime shifts, microstructure "
            "change, and other participants all eat at it. Assume half-life "
            "and plan exit BEFORE entry."
        ),
        bias=-0.20,
        violation_code="edge_decay_ignored",
        applies_to=("strategy.*", "framework.autopilot"),
    ),
    Tenet.PROCESS_OVER_OUTCOME: TenetSpec(
        tenet=Tenet.PROCESS_OVER_OUTCOME,
        statement=(
            "A losing trade that honored the plan is a SUCCESS. A winning "
            "trade that violated the plan is a FAILURE. Grade the process."
        ),
        bias=+0.10,
        violation_code="outcome_over_process",
        applies_to=("firm.red_team", "firm.pm"),
    ),
    Tenet.KAIZEN: TenetSpec(
        tenet=Tenet.KAIZEN,
        statement=(
            "Continuous, compounding improvement. Every cycle ends with a "
            "retrospective. Every retrospective produces a +1 -- one "
            "concrete, shippable improvement. No exceptions."
        ),
        bias=+0.15,
        violation_code="kaizen_missed_cycle",
        applies_to=("watchdog.autopilot", "framework.meta_orchestrator"),
    ),
    Tenet.ADVERSARIAL_HONESTY: TenetSpec(
        tenet=Tenet.ADVERSARIAL_HONESTY,
        statement=(
            "Red-team your own data. The null hypothesis is that you have "
            "no edge. The burden of proof is ON YOU. Any result you want "
            "to be true gets the harshest review."
        ),
        bias=-0.25,
        violation_code="red_team_bypassed",
        applies_to=("firm.red_team", "firm.risk", "gates.chain"),
    ),
    Tenet.OBSERVABILITY: TenetSpec(
        tenet=Tenet.OBSERVABILITY,
        statement=(
            "If you didn't log it, it didn't happen. Every decision, every override, every fill. Audit trail is sacred."
        ),
        bias=+0.05,
        violation_code="audit_gap",
        applies_to=("*",),  # every subsystem
    ),
    Tenet.NEVER_ON_AUTOPILOT: TenetSpec(
        tenet=Tenet.NEVER_ON_AUTOPILOT,
        statement=(
            "Human in the loop at every tier. Full-auto is a comfort "
            "illusion. JARVIS is the watchdog, not the replacement."
        ),
        bias=-0.15,
        violation_code="autopilot_unchecked",
        applies_to=("framework.autopilot", "bot.*"),
    ),
}

# Ordered priority: earlier tenets trump later ones when they conflict.
PRIORITY_ORDER: tuple[Tenet, ...] = (
    Tenet.CAPITAL_FIRST,
    Tenet.NEVER_ON_AUTOPILOT,
    Tenet.ADVERSARIAL_HONESTY,
    Tenet.EDGE_IS_FRAGILE,
    Tenet.OBSERVABILITY,
    Tenet.KAIZEN,
    Tenet.PROCESS_OVER_OUTCOME,
)


class DoctrineVerdict(BaseModel):
    """Outcome of applying the doctrine to a proposed action."""

    model_config = ConfigDict(frozen=True)

    proposed_verdict: str
    doctrine_verdict: str
    net_bias: float = Field(ge=-1.0, le=1.0)
    tenets_applied: list[str]
    violations: list[str] = Field(default_factory=list)
    rationale: str
    ts: datetime


def apply_doctrine(
    *,
    proposed_verdict: str,
    subsystem: str,
    action: str,
    context_tags: list[str] | None = None,
    violations: list[str] | None = None,
    now: datetime | None = None,
) -> DoctrineVerdict:
    """Fold the doctrine into a proposed verdict.

    Each applicable tenet contributes its ``bias``. Sum them, threshold
    the result:
      * net_bias <= -0.30 -> downgrade verdict one tier
      * net_bias >=  0.25 -> upgrade verdict one tier
      * else              -> unchanged

    Returned verdict is uppercase string (APPROVED / CONDITIONAL / DENIED
    / DEFERRED).
    """
    context_tags = context_tags or []
    violations = list(violations or [])
    applied: list[str] = []
    net = 0.0

    for t in PRIORITY_ORDER:
        spec = DOCTRINE[t]
        if _applies(subsystem, spec.applies_to):
            net += spec.bias
            applied.append(t.value)

    net = max(-1.0, min(1.0, net))

    ladder = ["DENIED", "DEFERRED", "CONDITIONAL", "APPROVED"]
    try:
        idx = ladder.index(proposed_verdict.upper())
    except ValueError:
        idx = ladder.index("CONDITIONAL")

    if net <= -0.30 and idx > 0:
        new_idx = idx - 1
    elif net >= 0.25 and idx < len(ladder) - 1:
        new_idx = idx + 1
    else:
        new_idx = idx
    new_verdict = ladder[new_idx]

    rationale = f"doctrine bias={net:+.2f}; {len(applied)} tenets applied; {proposed_verdict.upper()} -> {new_verdict}"

    return DoctrineVerdict(
        proposed_verdict=proposed_verdict.upper(),
        doctrine_verdict=new_verdict,
        net_bias=round(net, 4),
        tenets_applied=applied,
        violations=violations,
        rationale=rationale,
        ts=now or datetime.now(UTC),
    )


def _applies(subsystem: str, patterns: tuple[str, ...]) -> bool:
    """Glob-ish pattern match -- 'bot.*' matches 'bot.mnq', etc."""
    for p in patterns:
        if p == "*":
            return True
        if p.endswith(".*"):
            prefix = p[:-2]
            if subsystem.startswith(prefix + ".") or subsystem == prefix:
                return True
        elif p == subsystem:
            return True
    return False


def kaizen_pre_condition(
    retrospectives_last_7d: int,
    min_required: int = 7,
) -> tuple[bool, str]:
    """Is the KAIZEN tenet honored this cycle?

    Doctrine requires one retrospective per day (plus any extra triggered
    by incidents). ``min_required`` defaults to 7/week.
    """
    if retrospectives_last_7d >= min_required:
        return True, f"{retrospectives_last_7d} retrospectives in last 7d -- KAIZEN honored"
    return (
        False,
        f"only {retrospectives_last_7d} retrospectives in last 7d (need {min_required}) -- KAIZEN breached",
    )


def summarize_doctrine() -> str:
    """Human-readable dump of the full doctrine -- for dashboards / docs."""
    lines = ["EVOLUTIONARY TRADING ALGO DOCTRINE", "=" * 22]
    for t in PRIORITY_ORDER:
        spec = DOCTRINE[t]
        lines.append(f"\n[{t.value}]  bias={spec.bias:+.2f}")
        lines.append(f"  {spec.statement}")
    return "\n".join(lines)
