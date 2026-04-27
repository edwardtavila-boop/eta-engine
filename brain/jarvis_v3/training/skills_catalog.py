"""
JARVIS v3 // training.skills_catalog
====================================
Every TaskCategory each persona can handle, with capability notes.

This is the canonical "what can X do" registry. The dispatcher consults
it when routing; the dashboard renders it per persona; the eval harness
uses it as a ground truth.

Data is deliberately human-readable -- operator edits this file to grow
the fleet's capabilities. Code just reads.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.model_policy import TaskCategory


class Skill(BaseModel):
    """One skill a persona can perform."""

    model_config = ConfigDict(frozen=True)

    category: TaskCategory
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    success_example: str = Field(default="")
    typical_tokens: int = Field(ge=0, default=800)
    mastery: str = Field(pattern="^(core|strong|emerging)$", default="strong")


# Master catalog, keyed by persona name
PERSONA_SKILLS: dict[str, list[Skill]] = {
    "BATMAN": [
        Skill(
            category=TaskCategory.RED_TEAM_SCORING,
            title="Red-team scoring of a promotion candidate",
            summary=(
                "Enumerate attack vectors, evidence-check each, propose "
                "mitigations, issue PROMOTE/ITERATE/KILL verdict."
            ),
            success_example=(
                "5 vectors identified, 2 survive evidence, mitigations "
                "land, verdict ITERATE with named gate conditions."
            ),
            typical_tokens=1200,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.GAUNTLET_GATE_DESIGN,
            title="Design a paper->live promotion gate ladder",
            summary=("Define 5+ numeric gates with fail-closed defaults so a single failing metric blocks promotion."),
            typical_tokens=1500,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.RISK_POLICY_DESIGN,
            title="Kill-switch + sizing + tier-rollout policy",
            summary="Draft policies with explicit thresholds + rollback conditions.",
            typical_tokens=1200,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.ARCHITECTURE_DECISION,
            title="Module boundary / hot-path architecture call",
            summary=(
                "ADR-style: context, decision, consequences. Identify architectural attack vectors before shipping."
            ),
            typical_tokens=1000,
            mastery="strong",
        ),
        Skill(
            category=TaskCategory.ADVERSARIAL_REVIEW,
            title="Devil's-advocate pass on any artifact",
            summary="Steelman first, attack second, mitigations third.",
            typical_tokens=900,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.STATE_MACHINE_DESIGN,
            title="Regime / circuit-breaker state machine",
            summary=("State enum, transition table, guard conditions, rollback paths, audit log shape."),
            typical_tokens=1100,
            mastery="strong",
        ),
    ],
    "ALFRED": [
        Skill(
            category=TaskCategory.STRATEGY_EDIT,
            title="Confluence / sweep / ORB parameter tweak",
            summary="Small, reversible edits to strategy configs + code.",
            success_example=(
                "Plan: 3 steps. Deliverable: diff for configs/orb_tight.yaml + "
                "strategies/orb.py. Check: pytest tests/test_orb.py."
            ),
            typical_tokens=700,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.TEST_RUN,
            title="Write + run pytest suite",
            summary="Property-based + edge + invariant tests.",
            typical_tokens=800,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.REFACTOR,
            title="Rename / move / extract",
            summary="Backward-compatible diffs, deprecation shims, staged rollout.",
            typical_tokens=700,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.SKELETON_SCAFFOLD,
            title="New module skeleton with docstrings + stubs",
            summary="Ready-to-fill, tests green, imports resolve.",
            typical_tokens=600,
            mastery="strong",
        ),
        Skill(
            category=TaskCategory.CODE_REVIEW,
            title="Non-adversarial code review",
            summary="Style, correctness, naming, test coverage, imports.",
            typical_tokens=600,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.DEBUG,
            title="Fix a failing test / bug",
            summary="Hypothesis, isolate, fix, regression test.",
            typical_tokens=800,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.DOC_WRITING,
            title="CLAUDE.md / README / runbook updates",
            summary="Plain voice, operator-actionable, examples > prose.",
            typical_tokens=700,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.DATA_PIPELINE,
            title="Databento / parquet / Arctic plumbing",
            summary=("Schema-aware ingest, idempotent writes, backfill safety."),
            typical_tokens=800,
            mastery="strong",
        ),
    ],
    "ROBIN": [
        Skill(
            category=TaskCategory.LOG_PARSING,
            title="Tail + grep + summarize",
            summary="Counter over error types, latest N lines, one-line summary.",
            typical_tokens=200,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.SIMPLE_EDIT,
            title="Rename var / fix typo",
            summary="Exact diff, no prose.",
            typical_tokens=150,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.COMMIT_MESSAGE,
            title="Draft commit from a diff",
            summary="Conventional Commits style. Under 80 chars subject + optional body.",
            typical_tokens=120,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.FORMATTING,
            title="Imports + whitespace + trailing newline",
            summary="Ruff-aligned, no behavior change.",
            typical_tokens=100,
            mastery="strong",
        ),
        Skill(
            category=TaskCategory.LINT_FIX,
            title="Ruff / mypy mechanical fixes",
            summary="Shortest possible diff; no refactors snuck in.",
            typical_tokens=200,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.TRIVIAL_LOOKUP,
            title="Find file / find symbol",
            summary="Just the answer; no explanation unless asked.",
            typical_tokens=80,
            mastery="core",
        ),
        Skill(
            category=TaskCategory.BOILERPLATE,
            title="__init__.py re-exports + __all__",
            summary="Alphabetical, groups kept, no stray imports.",
            typical_tokens=150,
            mastery="strong",
        ),
    ],
    # JARVIS has no LLM skills -- he's deterministic. But we enumerate his
    # runtime capabilities for dashboard symmetry.
    "JARVIS": [
        Skill(
            category=TaskCategory.STRATEGY_EDIT,
            title="Deterministic verdict on risk-adding action",
            summary=(
                "JARVIS never runs an LLM. He runs the jarvis_admin policy "
                "engine, returns ActionResponse with verdict + reason_code."
            ),
            typical_tokens=0,
            mastery="core",
        ),
    ],
}


def skills_for(persona: str) -> list[Skill]:
    """Return the skill list for a persona (case-insensitive)."""
    key = persona.upper()
    return PERSONA_SKILLS.get(key, [])


def can_handle(persona: str, category: TaskCategory) -> bool:
    """True if the persona has this TaskCategory in its skill list."""
    return any(s.category == category for s in skills_for(persona))


def categories_by_persona() -> dict[str, list[str]]:
    """persona -> [category.value] rollup used by the dashboard."""
    return {persona: [s.category.value for s in skills] for persona, skills in PERSONA_SKILLS.items()}


def persona_for_category(category: TaskCategory) -> str | None:
    """Which persona owns this task category (first match wins)."""
    for persona, skills in PERSONA_SKILLS.items():
        if persona == "JARVIS":
            continue  # JARVIS is deterministic; skip when routing LLM work
        for skill in skills:
            if skill.category == category:
                return persona
    return None
