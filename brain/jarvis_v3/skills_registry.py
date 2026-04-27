"""
JARVIS v3 // skills_registry
============================
Every local skill callable through JARVIS.

Claude Code skills are powerful but unscoped by default -- any session can
invoke any skill. The Evolutionary Trading Algo Core enforces scoping: JARVIS owns
the registry of available skills, maps each to a SubsystemId + risk tier,
and gates invocation through ``JarvisAdmin.request_approval``.

This module provides:

  * ``SkillTier``       -- LOW / MEDIUM / HIGH (risk)
  * ``SkillDescriptor`` -- name + tier + allowed subsystems + human_doc
  * ``SkillRegistry``   -- in-memory catalog with JSON persistence
  * ``can_invoke``      -- pure guard: returns (allowed, reason)

The ACTUAL invocation still happens in the Claude harness via the Skill
tool -- this registry is the allowlist / audit layer.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SkillTier(StrEnum):
    """Risk tier of a skill. Higher tier -> stricter approval flow."""

    LOW = "LOW"  # read-only, idempotent (bot-status, firm-status)
    MEDIUM = "MEDIUM"  # writes to disk but reversible
    HIGH = "HIGH"  # destructive / irreversible (deploy, kill)


class SkillDescriptor(BaseModel):
    """One skill entry."""

    model_config = ConfigDict(frozen=False)

    name: str = Field(min_length=1)
    tier: SkillTier
    allowed_subsystems: list[str] = Field(default_factory=lambda: ["operator.edward"])
    description: str = ""
    categories: list[str] = Field(default_factory=list)
    # If true, every invocation is logged even if tier is LOW.
    always_audit: bool = True
    # How much of the doctrine bias applies (0.0 = none, 1.0 = full).
    doctrine_weight: float = Field(default=1.0, ge=0.0, le=1.0)


class SkillInvocationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    skill: str
    allowed: bool
    reason: str
    audit_ref: str = ""


class SkillRegistry:
    """Canonical list of skills JARVIS knows about + who may call them."""

    def __init__(self) -> None:
        self._by_name: dict[str, SkillDescriptor] = {}

    def register(self, d: SkillDescriptor) -> None:
        self._by_name[d.name] = d

    def get(self, name: str) -> SkillDescriptor | None:
        return self._by_name.get(name)

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def by_tier(self, tier: SkillTier) -> list[SkillDescriptor]:
        return [d for d in self._by_name.values() if d.tier == tier]

    def can_invoke(
        self,
        skill: str,
        subsystem: str,
    ) -> SkillInvocationResult:
        d = self._by_name.get(skill)
        if d is None:
            return SkillInvocationResult(
                skill=skill,
                allowed=False,
                reason=f"skill '{skill}' not registered",
            )
        if _matches_any(subsystem, d.allowed_subsystems):
            return SkillInvocationResult(
                skill=skill,
                allowed=True,
                reason=f"{subsystem} on allowlist for {skill} (tier={d.tier.value})",
            )
        return SkillInvocationResult(
            skill=skill,
            allowed=False,
            reason=f"{subsystem} not in allowlist for {skill}",
        )

    # Persistence -------------------------------------------------------
    def save(self, path: Path | str) -> None:
        out = {"skills": [d.model_dump() for d in self._by_name.values()]}
        Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> SkillRegistry:
        p = Path(path)
        if not p.exists():
            return default_registry()
        data = json.loads(p.read_text(encoding="utf-8"))
        reg = cls()
        for d in data.get("skills", []):
            reg.register(SkillDescriptor.model_validate(d))
        return reg


def _matches_any(subsystem: str, patterns: list[str]) -> bool:
    for p in patterns:
        if p == "*":
            return True
        if p == subsystem:
            return True
        if p.endswith(".*"):
            prefix = p[:-2]
            if subsystem.startswith(prefix + ".") or subsystem == prefix:
                return True
    return False


def default_registry() -> SkillRegistry:
    """Opinionated default set covering every skill visible on the Firm stack.

    The Claude Code session lists these in its system reminder; this
    registry mirrors them with scoping. Keep in sync with the skill set.
    """
    reg = SkillRegistry()

    # === Status / observability (LOW) ==========================================
    for name in (
        "bot-status",
        "bot-update",
        "firm-status",
        "board-status",
        "pdf-viewer:open",
        "pdf-viewer:annotate",
    ):
        reg.register(
            SkillDescriptor(
                name=name,
                tier=SkillTier.LOW,
                allowed_subsystems=["operator.edward", "watchdog.autopilot"],
                description=f"Read-only status/view skill: {name}",
                categories=["observability"],
            )
        )

    # === Research / analysis (MEDIUM) =========================================
    for name in (
        "product-management:brainstorm",
        "superpowers:brainstorm",
        "superpowers:brainstorming",
        "data:analyze",
        "data:build-dashboard",
        "data:explore-data",
        "claude-api",
    ):
        reg.register(
            SkillDescriptor(
                name=name,
                tier=SkillTier.MEDIUM,
                allowed_subsystems=["operator.edward"],
                description=f"Research / analysis skill: {name}",
                categories=["research"],
            )
        )

    # === Board orchestration (MEDIUM) =========================================
    for name in ("board-start", "board-iterate", "board-promote"):
        reg.register(
            SkillDescriptor(
                name=name,
                tier=SkillTier.MEDIUM,
                allowed_subsystems=["operator.edward"],
                description=f"Quant board cycle: {name}",
                categories=["strategy"],
            )
        )

    # === Firm / trading (HIGH) ================================================
    reg.register(
        SkillDescriptor(
            name="firm:the-firm",
            tier=SkillTier.HIGH,
            allowed_subsystems=["operator.edward", "firm.pm"],
            description="Adversarial firm review of a strategy; can alter gates",
            categories=["trading", "risk"],
        )
    )

    # === Engineering (MEDIUM) =================================================
    for name in (
        "engineering:code-review",
        "engineering:debug",
        "engineering:deploy-checklist",
        "engineering:architecture",
        "engineering:incident-response",
        "superpowers:receiving-code-review",
        "superpowers:requesting-code-review",
    ):
        reg.register(
            SkillDescriptor(
                name=name,
                tier=SkillTier.MEDIUM,
                allowed_subsystems=["operator.edward"],
                description=f"Engineering skill: {name}",
                categories=["engineering"],
            )
        )

    # === Ops (HIGH) ===========================================================
    for name in (
        "operations:change-request",
        "operations:risk-assessment",
        "operations:runbook",
    ):
        reg.register(
            SkillDescriptor(
                name=name,
                tier=SkillTier.HIGH,
                allowed_subsystems=["operator.edward"],
                description=f"Ops skill: {name}",
                categories=["ops"],
            )
        )

    # === Setup / meta =========================================================
    for name in ("update-config", "keybindings-help", "schedule", "loop"):
        reg.register(
            SkillDescriptor(
                name=name,
                tier=SkillTier.MEDIUM,
                allowed_subsystems=["operator.edward"],
                description=f"Harness meta-skill: {name}",
                categories=["meta"],
            )
        )

    return reg
