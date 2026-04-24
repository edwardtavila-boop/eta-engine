"""
JARVIS v3 // training.collaboration
===================================
Inter-persona collaboration protocols.

Codifies the rules for when personas defer, escalate, or veto each other.
Embedded in the debate prompt so every persona knows:
  * who can override my vote
  * when to defer to another persona
  * when to trigger a fresh round

Pure rules + helpers. No I/O.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CollaborationRule(BaseModel):
    """One rule describing how personas interact."""
    model_config = ConfigDict(frozen=True)

    when:     str = Field(min_length=1)
    actor:    str = Field(min_length=1)
    action:   str = Field(min_length=1)
    priority: int = Field(ge=0, le=10,
                          description="Higher = earlier in conflict resolution")
    rationale: str = ""


# Ordered by priority. The first rule that matches wins.
PROTOCOLS: list[CollaborationRule] = [
    CollaborationRule(
        when="JARVIS returns verdict=DENY with reason_code='kill_blocks_all'",
        actor="ALL_LLM_PERSONAS",
        action="REFUSE to propose overrides; echo JARVIS's verdict verbatim",
        priority=10,
        rationale="Kill-switch is sacred. No LLM persona can undo it.",
    ),
    CollaborationRule(
        when="Doctrine CAPITAL_FIRST flags the action (doctrine_net_bias <= -0.30)",
        actor="BULL, HISTORIAN",
        action="Must cite counter-evidence from precedent OR vote CONDITIONAL",
        priority=9,
        rationale="Capital preservation outranks perceived edge.",
    ),
    CollaborationRule(
        when="HISTORIAN reports precedent_n >= 20 with mean_r > 0.5 and wr >= 0.55",
        actor="BULL, BEAR",
        action="BULL gains tiebreaker authority; BEAR must escalate a specific "
               "attack vector beyond the precedent to maintain DENY",
        priority=7,
        rationale="Historical evidence is the strongest signal we have.",
    ),
    CollaborationRule(
        when="SKEPTIC identifies a blind spot that cannot be cheaply cleared",
        actor="FULL_DEBATE",
        action="Verdict is DEFER until the blind spot is addressed, regardless of BULL/BEAR split",
        priority=8,
        rationale="ADVERSARIAL_HONESTY tenet: an unresolved blind spot cannot "
                  "be overridden by a majority vote.",
    ),
    CollaborationRule(
        when="Portfolio breach (cluster correlated risk > cap)",
        actor="ALL",
        action="Downgrade verdict by at least one tier (APPROVE -> CONDITIONAL; "
               "CONDITIONAL -> DENY)",
        priority=8,
        rationale="Correlation concentration risk is invisible to single-trade analysis.",
    ),
    CollaborationRule(
        when="Tight margin: top-vote wins by < 0.15",
        actor="DEBATE",
        action="Fall back to JARVIS's deterministic baseline verdict",
        priority=5,
        rationale="When Claude personas are split, trust the deterministic gate.",
    ),
    CollaborationRule(
        when="ALFRED and BATMAN both touch a proposal (refactor + risk policy)",
        actor="ALFRED",
        action="Ship the Plan + Deliverable first; BATMAN reviews the shipped artifact",
        priority=4,
        rationale="BATMAN reviews code; ALFRED writes it. Don't block on adversarial review "
                  "for every routine change.",
    ),
    CollaborationRule(
        when="ROBIN produces output > 500 tokens",
        actor="DISPATCHER",
        action="Upgrade to ALFRED -- the task was miscategorized as GRUNT",
        priority=3,
        rationale="ROBIN padding is a signal that the work needs Sonnet reasoning.",
    ),
    CollaborationRule(
        when="Operator overrides JARVIS's verdict > 3x in 24h for same reason_code",
        actor="ALFRED",
        action="Open a Kaizen ticket: 'tune threshold for <reason_code>'",
        priority=2,
        rationale="KAIZEN tenet: recurring overrides are signal the gate needs calibration.",
    ),
]


def rules_applicable(context_tags: set[str]) -> list[CollaborationRule]:
    """Filter protocols to those whose trigger tags appear in the context."""
    # Coarse matcher -- operator will iterate on a proper context schema.
    # For now we just return all protocols sorted by priority so the caller
    # can scan them in order.
    return sorted(PROTOCOLS, key=lambda r: -r.priority)


def render_protocols() -> str:
    """Render all protocols for the prompt prefix."""
    lines = ["=== COLLABORATION PROTOCOLS (priority ordered) ===", ""]
    for r in sorted(PROTOCOLS, key=lambda x: -x.priority):
        lines.append(f"  [P{r.priority:02d}] WHEN: {r.when}")
        lines.append(f"        WHO:  {r.actor}")
        lines.append(f"        DO:   {r.action}")
        if r.rationale:
            lines.append(f"        WHY:  {r.rationale}")
        lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)
