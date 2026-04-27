"""
JARVIS v3 // claude_layer.escalation
====================================
Layer 1 -- tiered escalation gate.

Default: JARVIS handles it (free). Only escalate to Claude when one
or more hard triggers fire. Each trigger has a reason_code so the
cost governor can show WHY Claude was invoked.

Escalation triggers (hand-tuned; each is deterministic):

  * CRISIS regime
  * stress_composite >= 0.55
  * sizing_mult <= 0.40 (ie deep REDUCE tier)
  * macro event <= 1h away
  * portfolio cluster breach
  * doctrine net_bias <= -0.30 (strong downgrade signal)
  * action in {STRATEGY_DEPLOY, KILL_SWITCH_RESET, GATE_OVERRIDE}
  * R-at-risk > 2.0
  * operator override velocity > 3/24h (operator fighting JARVIS a lot)
  * precedent bucket empty (first-time scenario)

If NONE fire, JARVIS's in-house deterministic debate is sufficient.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EscalationTrigger(StrEnum):
    """Stable codes -- logged + dashboarded."""

    CRISIS_REGIME = "CRISIS_REGIME"
    HIGH_STRESS = "HIGH_STRESS"
    LOW_SIZING = "LOW_SIZING"
    EVENT_IMMINENT = "EVENT_IMMINENT"
    PORTFOLIO_BREACH = "PORTFOLIO_BREACH"
    DOCTRINE_CONFLICT = "DOCTRINE_CONFLICT"
    CRITICAL_ACTION = "CRITICAL_ACTION"
    HIGH_R_AT_RISK = "HIGH_R_AT_RISK"
    OPERATOR_OVERRIDE_HOT = "OPERATOR_OVERRIDE_HOT"
    PRECEDENT_EMPTY = "PRECEDENT_EMPTY"
    ANOMALY_DETECTED = "ANOMALY_DETECTED"
    FIRM_APPEAL = "FIRM_APPEAL"


class EscalationDecision(BaseModel):
    """Output of ``should_escalate``."""

    model_config = ConfigDict(frozen=True)

    escalate: bool
    triggers: list[EscalationTrigger] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    jarvis_handles: bool
    note: str


class EscalationInputs(BaseModel):
    """Pydantic bundle of every signal the gate reads."""

    model_config = ConfigDict(frozen=True)

    regime: str = "NEUTRAL"
    stress_composite: float = Field(ge=0.0, le=1.0, default=0.0)
    sizing_mult: float = Field(ge=0.0, le=1.0, default=1.0)
    hours_until_event: float | None = None
    portfolio_breach: bool = False
    doctrine_net_bias: float = Field(ge=-1.0, le=1.0, default=0.0)
    action: str = ""
    r_at_risk: float = Field(ge=0.0, default=0.0)
    operator_overrides_24h: int = Field(ge=0, default=0)
    precedent_n: int = Field(ge=0, default=0)
    anomaly_count: int = Field(ge=0, default=0)
    firm_appeal_active: bool = False


# Threshold block (module-level so tests can monkey-patch).
STRESS_ESCALATE = 0.55
SIZING_ESCALATE = 0.40
EVENT_IMMINENT_H = 1.0
DOCTRINE_CONFLICT_BIAS = -0.30
R_AT_RISK_ESCALATE = 2.0
OVERRIDE_HOT_24H = 3

# Actions that ALWAYS escalate, regardless of context.
_CRITICAL_ACTIONS: frozenset[str] = frozenset(
    {
        "STRATEGY_DEPLOY",
        "KILL_SWITCH_RESET",
        "GATE_OVERRIDE",
        "CAPITAL_ALLOCATE",
    }
)


def should_escalate(inp: EscalationInputs) -> EscalationDecision:
    """Pure gate: should we invoke Claude for this decision?"""
    triggers: list[EscalationTrigger] = []
    reasons: list[str] = []

    if inp.regime.upper() in {"CRISIS"}:
        triggers.append(EscalationTrigger.CRISIS_REGIME)
        reasons.append("regime=CRISIS -- Claude arbitrates")
    if inp.stress_composite >= STRESS_ESCALATE:
        triggers.append(EscalationTrigger.HIGH_STRESS)
        reasons.append(f"stress {inp.stress_composite:.2f} >= {STRESS_ESCALATE}")
    if inp.sizing_mult <= SIZING_ESCALATE:
        triggers.append(EscalationTrigger.LOW_SIZING)
        reasons.append(f"sizing_mult {inp.sizing_mult:.2f} <= {SIZING_ESCALATE}")
    if inp.hours_until_event is not None and 0 <= inp.hours_until_event <= EVENT_IMMINENT_H:
        triggers.append(EscalationTrigger.EVENT_IMMINENT)
        reasons.append(f"event in {inp.hours_until_event:.2f}h")
    if inp.portfolio_breach:
        triggers.append(EscalationTrigger.PORTFOLIO_BREACH)
        reasons.append("portfolio cluster breach")
    if inp.doctrine_net_bias <= DOCTRINE_CONFLICT_BIAS:
        triggers.append(EscalationTrigger.DOCTRINE_CONFLICT)
        reasons.append(f"doctrine bias {inp.doctrine_net_bias:+.2f} <= {DOCTRINE_CONFLICT_BIAS}")
    if inp.action in _CRITICAL_ACTIONS:
        triggers.append(EscalationTrigger.CRITICAL_ACTION)
        reasons.append(f"{inp.action} -- always escalates")
    if inp.r_at_risk > R_AT_RISK_ESCALATE:
        triggers.append(EscalationTrigger.HIGH_R_AT_RISK)
        reasons.append(f"R-at-risk {inp.r_at_risk:.2f} > {R_AT_RISK_ESCALATE}")
    if inp.operator_overrides_24h >= OVERRIDE_HOT_24H:
        triggers.append(EscalationTrigger.OPERATOR_OVERRIDE_HOT)
        reasons.append(f"operator overrode {inp.operator_overrides_24h}x in 24h")
    if inp.precedent_n == 0:
        triggers.append(EscalationTrigger.PRECEDENT_EMPTY)
        reasons.append("no precedent for this bucket -- need reasoning")
    if inp.anomaly_count > 0:
        triggers.append(EscalationTrigger.ANOMALY_DETECTED)
        reasons.append(f"{inp.anomaly_count} input(s) flagged by anomaly")
    if inp.firm_appeal_active:
        triggers.append(EscalationTrigger.FIRM_APPEAL)
        reasons.append("court-of-appeals case in progress")

    escalate = bool(triggers)
    if escalate:
        note = f"escalating to Claude ({len(triggers)} trigger(s))"
    else:
        note = "JARVIS handles this locally (all triggers quiet)"
    return EscalationDecision(
        escalate=escalate,
        triggers=triggers,
        reasons=reasons,
        jarvis_handles=not escalate,
        note=note,
    )


def escalation_rate(
    decisions: list[EscalationDecision],
) -> tuple[int, int, float]:
    """Convenience: count (escalated, total, rate) over a batch."""
    total = len(decisions)
    if total == 0:
        return 0, 0, 0.0
    esc = sum(1 for d in decisions if d.escalate)
    return esc, total, round(esc / total, 4)
