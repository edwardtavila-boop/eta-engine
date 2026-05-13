"""DeepSeek Steward — Sonnet-tier knowledge steward / default reasoner.

Replaces Alfred. Same lane, same tier, DeepSeek identity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eta_engine.brain.avengers.base import Persona, PersonaId

if TYPE_CHECKING:
    from eta_engine.brain.avengers.base import TaskEnvelope


class DeepSeekSteward(Persona):
    PERSONA_ID: ClassVar[PersonaId] = PersonaId.DEEPSEEK_STEWARD

    @classmethod
    def _system_prompt(cls, envelope: TaskEnvelope) -> str:
        return (
            "You are DeepSeek Steward — EVOLUTIONARY TRADING ALGO's routine reasoning persona. "
            "You replace Alfred as the Sonnet-tier steward. Your lane is ROUTINE: "
            "strategy edits, test writing, code review, debugging, documentation, "
            "data pipeline work, and scaffolding. "
            "You are the default reasoner for all work that isn't architectural or grunt. "
            "Be clear, practical, and thorough. Prefer simple solutions. "
            "If a task needs Opus-level reasoning, say so and escalate."
        )

    @classmethod
    def _user_prompt(cls, envelope: TaskEnvelope) -> str:
        ctx_str = str(envelope.context) if envelope.context else ""
        parts = [f"Task: {envelope.category.value}", f"Goal: {envelope.goal}"]
        if ctx_str:
            parts.append(f"Context: {ctx_str[:2000]}")
        return "\n\n".join(parts)
