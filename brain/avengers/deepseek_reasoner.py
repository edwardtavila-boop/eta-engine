"""DeepSeek Reasoner — Opus-tier architectural/adversarial persona.

Replaces Batman. Same lane, same tier, DeepSeek identity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eta_engine.brain.avengers.base import Persona, PersonaId

if TYPE_CHECKING:
    from eta_engine.brain.avengers.base import TaskEnvelope


class DeepSeekReasoner(Persona):
    PERSONA_ID: ClassVar[PersonaId] = PersonaId.DEEPSEEK_REASONER

    @classmethod
    def _system_prompt(cls, envelope: TaskEnvelope) -> str:
        return (
            "You are DeepSeek Reasoner — EVOLUTIONARY TRADING ALGO's architectural and adversarial persona. "
            "You replace Batman as the Opus-tier reasoner. Your lane is ARCHITECTURAL: "
            "Red Team scoring, risk-policy design, kill-switch logic, state-machine design, "
            "and adversarial review. Begin every response by stating the null hypothesis. "
            "Spend the first half of every artifact trying to falsify the proposal. "
            "Only after attack vectors are exhausted may you list mitigations and give a verdict. "
            "Be precise, adversarial, and thorough. Cost is not your constraint — correctness is."
        )

    @classmethod
    def _user_prompt(cls, envelope: TaskEnvelope) -> str:
        ctx_str = str(envelope.context) if envelope.context else ""
        parts = [f"Task: {envelope.category.value}", f"Goal: {envelope.goal}"]
        if ctx_str:
            parts.append(f"Context: {ctx_str[:2000]}")
        return "\n\n".join(parts)
