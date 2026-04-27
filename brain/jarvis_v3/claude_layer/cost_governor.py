"""
JARVIS v3 // claude_layer.cost_governor
=======================================
The integrated controller.

Combines all four layers into a single ``should_invoke_claude()``
decision that callers hit once per request. Layered short-circuits
mean each cheaper layer can veto before the next runs:

  Layer 1 (escalation)    -- MUST escalate, else return early
  Layer 4 (distillation)  -- if classifier says "skip", return early
  Quota (usage_tracker)   -- if FREEZE, return early
  Layer 3 (stakes)        -- picks the model tier
  Layer 2 (prompt_cache)  -- caller uses it when actually calling

Also exposes ``PersonaPlan``: given stakes + budget state, which of
the four personas run, and at what tier each. This is where we put
MORE STRESS ON JARVIS: under DOWNSHIFT or FREEZE, personas fall back
to their deterministic (free) implementations from ``next_level.debate``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.jarvis_v3.claude_layer.distillation import (
    Distiller,
    SkipDecision,
)
from eta_engine.brain.jarvis_v3.claude_layer.escalation import (
    EscalationDecision,
    EscalationInputs,
    should_escalate,
)
from eta_engine.brain.jarvis_v3.claude_layer.prompt_cache import (
    CACHE_READ_MULT,
    CACHE_WRITE_MULT,
    MODEL_PRICES,
)
from eta_engine.brain.jarvis_v3.claude_layer.stakes import (
    Stakes,
    StakesInputs,
    StakesVerdict,
    classify_stakes,
)
from eta_engine.brain.jarvis_v3.claude_layer.usage_tracker import (
    QuotaState,
    QuotaStatus,
    UsageTracker,
)
from eta_engine.brain.model_policy import ModelTier


class PersonaAssignment(BaseModel):
    """One persona's execution plan."""

    model_config = ConfigDict(frozen=True)

    persona: str
    tier: ModelTier | None = None  # None = run JARVIS-only (free)
    deterministic: bool = False
    reason: str = ""


class InvocationPlan(BaseModel):
    """The full decision the governor hands to the caller."""

    model_config = ConfigDict(frozen=True)

    invoke_claude: bool
    reason: str
    escalation: EscalationDecision
    stakes: StakesVerdict | None = None
    distillation: SkipDecision | None = None
    quota: QuotaStatus | None = None
    personas: list[PersonaAssignment] = Field(default_factory=list)
    est_cost_usd: float = Field(ge=0.0, default=0.0)


# ---------------------------------------------------------------------------
# Default persona plan per stakes. Under normal quota.
# ---------------------------------------------------------------------------


def _persona_plan_for_stakes(stakes: StakesVerdict) -> list[PersonaAssignment]:
    """Compose the default persona plan at a given stakes level."""
    s = stakes.stakes
    if s == Stakes.CRITICAL:
        # All four on Opus
        return [
            PersonaAssignment(persona="BULL", tier=ModelTier.OPUS, reason="CRITICAL -- full Opus quartet"),
            PersonaAssignment(persona="BEAR", tier=ModelTier.OPUS, reason="CRITICAL -- full Opus quartet"),
            PersonaAssignment(persona="SKEPTIC", tier=ModelTier.OPUS, reason="CRITICAL -- full Opus quartet"),
            PersonaAssignment(persona="HISTORIAN", tier=ModelTier.OPUS, reason="CRITICAL -- full Opus quartet"),
        ]
    if s == Stakes.HIGH:
        return [
            PersonaAssignment(persona="BULL", tier=ModelTier.SONNET, reason="HIGH -- Sonnet for PRO side"),
            PersonaAssignment(persona="BEAR", tier=ModelTier.SONNET, reason="HIGH -- Sonnet for CON side"),
            PersonaAssignment(persona="SKEPTIC", tier=ModelTier.OPUS, reason="HIGH -- Opus skeptic (adversarial)"),
            PersonaAssignment(persona="HISTORIAN", tier=ModelTier.HAIKU, reason="HIGH -- Haiku historian (citation)"),
        ]
    if s == Stakes.MEDIUM:
        # Only Skeptic + Historian fire on Claude; Bull/Bear run deterministic (free)
        return [
            PersonaAssignment(persona="BULL", deterministic=True, reason="MEDIUM -- JARVIS handles BULL"),
            PersonaAssignment(persona="BEAR", deterministic=True, reason="MEDIUM -- JARVIS handles BEAR"),
            PersonaAssignment(persona="SKEPTIC", tier=ModelTier.SONNET, reason="MEDIUM -- Sonnet skeptic"),
            PersonaAssignment(persona="HISTORIAN", tier=ModelTier.HAIKU, reason="MEDIUM -- Haiku historian"),
        ]
    # LOW: everyone deterministic -- should have been filtered earlier
    return [
        PersonaAssignment(persona=n, deterministic=True, reason="LOW -- JARVIS only")
        for n in ("BULL", "BEAR", "SKEPTIC", "HISTORIAN")
    ]


def _downshift_plan(plan: list[PersonaAssignment]) -> list[PersonaAssignment]:
    """Apply DOWNSHIFT: demote every non-deterministic tier by one step."""
    demote: dict[ModelTier, ModelTier] = {
        ModelTier.OPUS: ModelTier.SONNET,
        ModelTier.SONNET: ModelTier.HAIKU,
        ModelTier.HAIKU: ModelTier.HAIKU,  # can't go lower -- kept as Haiku
    }
    return [
        PersonaAssignment(
            persona=p.persona,
            tier=demote[p.tier] if p.tier else None,
            deterministic=p.deterministic,
            reason=f"DOWNSHIFT: {p.reason}",
        )
        for p in plan
    ]


def _freeze_plan(plan: list[PersonaAssignment]) -> list[PersonaAssignment]:
    """FREEZE: run every persona deterministically, no Claude."""
    return [
        PersonaAssignment(
            persona=p.persona,
            deterministic=True,
            reason="FREEZE: quota exhausted, JARVIS-only",
        )
        for p in plan
    ]


# ---------------------------------------------------------------------------
# Cost estimate for a persona plan (uses MODEL_PRICES imported above)
# ---------------------------------------------------------------------------


def _estimate_persona_cost(
    assignment: PersonaAssignment,
    prefix_tokens: int,
    suffix_tokens: int,
    output_tokens: int,
    cache_hit: bool,
) -> float:
    if assignment.deterministic or assignment.tier is None:
        return 0.0
    in_rate, out_rate = MODEL_PRICES[assignment.tier]
    if cache_hit:
        prefix_cost = prefix_tokens / 1_000_000 * in_rate * CACHE_READ_MULT
    else:
        prefix_cost = prefix_tokens / 1_000_000 * in_rate * CACHE_WRITE_MULT
    suffix_cost = suffix_tokens / 1_000_000 * in_rate
    output_cost = output_tokens / 1_000_000 * out_rate
    return round(prefix_cost + suffix_cost + output_cost, 6)


# ---------------------------------------------------------------------------
# Governor
# ---------------------------------------------------------------------------


class CostGovernor:
    """The single entrypoint the meta-controller hits per request."""

    def __init__(
        self,
        usage: UsageTracker,
        distiller: Distiller | None = None,
        skip_threshold: float = 0.92,
    ) -> None:
        self.usage = usage
        self.distiller = distiller or Distiller()
        self.skip_threshold = skip_threshold

    def plan(
        self,
        *,
        escalation_inputs: EscalationInputs,
        stakes_inputs: StakesInputs,
        features: dict[str, float],
        prefix_tokens: int = 500,
        suffix_tokens: int = 400,
        output_tokens: int = 300,
        expected_cache_hit: bool = True,
    ) -> InvocationPlan:
        """Produce the invocation plan for one request."""
        # Layer 1 -- escalate?
        esc = should_escalate(escalation_inputs)
        if not esc.escalate:
            return InvocationPlan(
                invoke_claude=False,
                reason="no escalation triggers -- JARVIS handles it",
                escalation=esc,
            )

        # Quota check BEFORE anything expensive
        q = self.usage.quota_state()
        if q.state == QuotaState.FREEZE:
            return InvocationPlan(
                invoke_claude=False,
                reason=f"quota FREEZE (hourly {q.hourly_pct:.0%}, daily {q.daily_pct:.0%})",
                escalation=esc,
                quota=q,
                personas=_freeze_plan(
                    _persona_plan_for_stakes(
                        classify_stakes(stakes_inputs),
                    ),
                ),
            )

        # Layer 4 -- distillation says skip?
        if self.distiller.model.train_n > 0:
            skip = self.distiller.should_skip(
                features,
                skip_threshold=self.skip_threshold,
            )
            if skip.skip_claude:
                return InvocationPlan(
                    invoke_claude=False,
                    reason="distillation says JARVIS is sufficient",
                    escalation=esc,
                    distillation=skip,
                    quota=q,
                )
        else:
            skip = None

        # Layer 3 -- stakes
        stk = classify_stakes(stakes_inputs)

        # Persona plan, then apply DOWNSHIFT if quota is at that level
        personas = _persona_plan_for_stakes(stk)
        if q.state == QuotaState.DOWNSHIFT:
            personas = _downshift_plan(personas)

        # Cost estimate
        total_cost = sum(
            _estimate_persona_cost(
                p,
                prefix_tokens,
                suffix_tokens,
                output_tokens,
                cache_hit=expected_cache_hit,
            )
            for p in personas
        )

        return InvocationPlan(
            invoke_claude=True,
            reason=(f"escalating -- stakes={stk.stakes.value}, quota={q.state.value}"),
            escalation=esc,
            stakes=stk,
            distillation=skip,
            quota=q,
            personas=personas,
            est_cost_usd=round(total_cost, 6),
        )
