"""
JARVIS v3 // next_level.autopr
==============================
Self-writing Kaizen PRs.

Every +1 Kaizen ticket gets a sub-agent draft: code change + tests +
PR body. Operator reviews and merges. The compounding-improvement
flywheel goes from "operator ships all +1's" to "operator reviews and
approves" -- 5-10x throughput for small-scope improvements.

This module contains the PURE planning layer:

  * ``PRPlan``           -- structured description of the change
  * ``estimate_scope``   -- ticket -> S/M/L/XL scope
  * ``select_model_tier`` -- which Claude tier to spawn (via bandit)
  * ``build_agent_prompt`` -- self-contained prompt for the sub-agent
  * ``submit_plan``       -- hand the plan to an executor callable

The EXECUTOR is injected -- actual sub-agent spawning + git PR ops live
in ``scripts/autopr_executor.py``. Keeps this module test-friendly.
"""

from __future__ import annotations

from collections.abc import Callable  # noqa: TC003  (used in type alias at runtime)
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.model_policy import ModelTier, TaskCategory, select_model

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.kaizen import KaizenTicket


class Scope(StrEnum):
    S = "S"  # < 1 hour, single file, clear diff
    M = "M"  # 1-4 hours, multiple files, tests required
    L = "L"  # half-day, cross-module change
    XL = "XL"  # multi-day; do NOT auto-PR, escalate to operator


class PRPlan(BaseModel):
    """Plan for one auto-generated PR."""

    model_config = ConfigDict(frozen=True)

    ticket_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    branch_name: str = Field(min_length=1)
    scope: Scope
    tier: ModelTier
    prompt: str = Field(min_length=20)
    proposed_files: list[str] = Field(default_factory=list)
    tests_required: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    estimated_hours: float = Field(ge=0.0)
    notes: str = ""


class AutoPRResult(BaseModel):
    """Outcome after an executor ran the plan."""

    model_config = ConfigDict(frozen=True)

    plan: PRPlan
    success: bool
    pr_url: str | None = None
    branch: str
    ts_started: datetime
    ts_finished: datetime
    message: str


# ---------------------------------------------------------------------------
# Scope estimation
# ---------------------------------------------------------------------------


def estimate_scope(ticket: KaizenTicket) -> Scope:
    """Rough scope estimation from the ticket's impact + title heuristics."""
    title = ticket.title.lower()
    if ticket.impact == "critical":
        return Scope.XL
    if ticket.impact == "large":
        return Scope.L
    if any(
        word in title
        for word in (
            "rewrite",
            "refactor module",
            "migrate",
            "architecture",
        )
    ):
        return Scope.L
    if any(
        word in title
        for word in (
            "fix typo",
            "rename",
            "lint",
            "docstring",
            "comment",
        )
    ):
        return Scope.S
    return Scope.M


def select_model_tier(scope: Scope) -> ModelTier:
    """Pick the model tier for a given scope via the single-source policy."""
    if scope == Scope.S:
        sel = select_model(TaskCategory.SIMPLE_EDIT)
    elif scope == Scope.M:
        sel = select_model(TaskCategory.REFACTOR)
    elif scope == Scope.L:
        sel = select_model(TaskCategory.CODE_REVIEW)
    else:
        sel = select_model(TaskCategory.ARCHITECTURE_DECISION)
    return sel.tier


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_agent_prompt(ticket: KaizenTicket) -> str:
    """Build a self-contained prompt for the sub-agent.

    The agent has no conversation context, so the prompt must include:
      * problem statement (from ticket.rationale)
      * exact acceptance criteria
      * test requirements
      * style + commit conventions
    """
    return (
        f"[KAIZEN TICKET {ticket.id}] {ticket.title}\n\n"
        f"## Rationale\n{ticket.rationale}\n\n"
        f"## Acceptance\n"
        f"- change must be in-scope for the titled fix only\n"
        f"- pytest must pass for all touched modules\n"
        f"- ruff must pass (rules per pyproject.toml)\n"
        f"- NO scope creep; if you discover related issues, open follow-up tickets\n\n"
        f"## Conventions\n"
        f"- module docstrings follow the JARVIS v3 style (see brain/jarvis_v3/)\n"
        f"- tests alongside implementation (tests/test_*.py)\n"
        f"- commit message format: `fix(<module>): <short imperative>`\n"
        f"  + full body explaining what + why\n"
        f"  + trailing `Co-Authored-By: Claude Opus 4.7 (1M context)`\n\n"
        f"## Output\n"
        f"- stage all changes\n"
        f"- write the commit\n"
        f"- return the branch name so the operator can open the PR\n"
    )


def build_plan(ticket: KaizenTicket, now: datetime | None = None) -> PRPlan:
    """Compose a PRPlan from a Kaizen ticket."""
    scope = estimate_scope(ticket)
    tier = select_model_tier(scope)
    hours = {Scope.S: 0.5, Scope.M: 2.0, Scope.L: 5.0, Scope.XL: 8.0}[scope]
    branch = f"kaizen/{ticket.id.lower()}"
    return PRPlan(
        ticket_id=ticket.id,
        title=ticket.title,
        branch_name=branch,
        scope=scope,
        tier=tier,
        prompt=build_agent_prompt(ticket),
        acceptance=[
            "tests pass",
            "ruff clean",
            "single-purpose change only",
        ],
        estimated_hours=hours,
        notes="XL scope requires operator review before auto-submit" if scope == Scope.XL else "",
    )


# ---------------------------------------------------------------------------
# Executor handoff (injected)
# ---------------------------------------------------------------------------

# Executor signature: (plan) -> (success, pr_url, branch, message)
Executor = Callable[[PRPlan], "AutoPRResult"]


def submit_plan(
    plan: PRPlan,
    executor: Executor | None,
    now: datetime | None = None,
) -> AutoPRResult:
    """Hand the plan to the executor. No executor -> dry-run result."""
    now = now or datetime.now(UTC)
    if plan.scope == Scope.XL:
        # XL scope always returns a dry-run "pending operator review"
        return AutoPRResult(
            plan=plan,
            success=False,
            pr_url=None,
            branch=plan.branch_name,
            ts_started=now,
            ts_finished=now,
            message="XL scope -- escalated to operator, NOT auto-submitted",
        )
    if executor is None:
        return AutoPRResult(
            plan=plan,
            success=False,
            pr_url=None,
            branch=plan.branch_name,
            ts_started=now,
            ts_finished=now,
            message="dry-run: no executor wired",
        )
    return executor(plan)
