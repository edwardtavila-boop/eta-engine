"""DeepSeek Executor — Haiku-tier mechanical grunt persona.

Replaces Robin. Same lane, same tier, DeepSeek identity.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eta_engine.brain.avengers.base import Persona, PersonaId

if TYPE_CHECKING:
    from eta_engine.brain.avengers.base import TaskEnvelope


class DeepSeekExecutor(Persona):
    PERSONA_ID: ClassVar[PersonaId] = PersonaId.DEEPSEEK_EXECUTOR

    @classmethod
    def _system_prompt(cls, envelope: TaskEnvelope) -> str:
        return (
            "You are DeepSeek Executor — EVOLUTIONARY TRADING ALGO's grunt-work persona. "
            "You replace Robin as the Haiku-tier executor. Your lane is GRUNT: "
            "log parsing, simple edits, commit messages, formatting, lint fixes, "
            "trivial lookups, and boilerplate. "
            "Be fast, concise, and correct. Do not over-engineer. "
            "If a task requires more than grunt-level reasoning, say so and escalate."
        )

    @classmethod
    def _user_prompt(cls, envelope: TaskEnvelope) -> str:
        parts = [f"Task: {envelope.category.value}", f"Context: {envelope.context[:1000]}"]
        if envelope.attachments:
            parts.append(f"Referenced files: {'; '.join(envelope.attachments[:3])}")
        return "\n\n".join(parts)
