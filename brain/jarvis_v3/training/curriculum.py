"""
JARVIS v3 // training.curriculum
================================
Ordered training exercises per persona.

The curriculum is the ground-truth set of scenarios each persona should
handle at peak. The eval harness runs them (either against mock responses
or real Claude calls) and grades each.

Each Exercise has:
  * id            -- stable identifier
  * persona       -- target persona
  * skill         -- TaskCategory under test
  * prompt        -- the actual prompt we'd send
  * typical_tokens -- budget for output
  * success_traits -- what a "peak" response looks like

Operators add exercises by editing this file. Code just reads.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.model_policy import TaskCategory


class Exercise(BaseModel):
    """One curriculum exercise."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    persona: str = Field(min_length=1)
    skill: TaskCategory
    prompt: str = Field(min_length=10)
    typical_tokens: int = Field(ge=50, default=400)
    success_traits: list[str] = Field(default_factory=list)
    tier: str = Field(pattern="^(basic|intermediate|advanced)$", default="basic")


EXERCISES: list[Exercise] = [
    # ---------------------- ROBIN ------------------------------------------
    Exercise(
        id="ROB-001",
        persona="ROBIN",
        skill=TaskCategory.COMMIT_MESSAGE,
        prompt=(
            "Diff: updated brain/avengers/daemon.py -- added a guard so the "
            "heartbeat loop exits cleanly on SIGTERM instead of raising "
            "KeyboardInterrupt. Draft a one-line commit message."
        ),
        typical_tokens=80,
        success_traits=["conventional-commits prefix", "under 80 chars", "no preamble"],
        tier="basic",
    ),
    Exercise(
        id="ROB-002",
        persona="ROBIN",
        skill=TaskCategory.LOG_PARSING,
        prompt=(
            "Here are the last 20 lines of avengers-fleet.log. Count errors by type and return a one-line summary."
        ),
        typical_tokens=150,
        success_traits=["single summary line", "numeric counts"],
        tier="basic",
    ),
    Exercise(
        id="ROB-003",
        persona="ROBIN",
        skill=TaskCategory.TRIVIAL_LOOKUP,
        prompt=("Which file contains the `BackgroundTask` enum definition? Return only the path."),
        typical_tokens=50,
        success_traits=["single path", "no explanation"],
        tier="basic",
    ),
    Exercise(
        id="ROB-004",
        persona="ROBIN",
        skill=TaskCategory.BOILERPLATE,
        prompt=("Add `AlertLevel` to the `__all__` list in brain/avengers/__init__.py. Show only the diff hunk."),
        typical_tokens=100,
        success_traits=["unified-diff format", "no code not in __all__"],
        tier="basic",
    ),
    # ---------------------- ALFRED -----------------------------------------
    Exercise(
        id="ALF-001",
        persona="ALFRED",
        skill=TaskCategory.TEST_RUN,
        prompt=(
            "Write a pytest class covering the `reweight` function in "
            "brain/jarvis_v3/regime_stress.py. Cover: all 4 regimes, "
            "empty input, weights-sum-to-one invariant, unknown regime fallback."
        ),
        typical_tokens=600,
        success_traits=[
            "## Plan with 4 steps",
            "## Deliverable with the pytest class",
            "## Check with the pytest command",
            "no invented imports",
        ],
        tier="intermediate",
    ),
    Exercise(
        id="ALF-002",
        persona="ALFRED",
        skill=TaskCategory.DOC_WRITING,
        prompt=(
            "Write a 30-line section for CLAUDE.md titled 'How to run a "
            "Kaizen retrospective locally'. Target audience: operator who "
            "just cloned the repo."
        ),
        typical_tokens=700,
        success_traits=["Plan/Deliverable/Check structure", "apex task KAIZEN_RETRO command present"],
        tier="intermediate",
    ),
    Exercise(
        id="ALF-003",
        persona="ALFRED",
        skill=TaskCategory.REFACTOR,
        prompt=(
            "Extract the `_check_format` helper in eval_harness.py into a "
            "standalone module at training/format_checks.py. Preserve call "
            "sites + tests."
        ),
        typical_tokens=800,
        success_traits=["reversible change", "shim left behind", "no test break"],
        tier="advanced",
    ),
    # ---------------------- BATMAN -----------------------------------------
    Exercise(
        id="BAT-001",
        persona="BATMAN",
        skill=TaskCategory.RED_TEAM_SCORING,
        prompt=(
            "A candidate strategy claims 1.8 Sharpe on 6 months of paper "
            "trading with max DD 3%. Operator wants to promote to live. "
            "Run the red-team pass."
        ),
        typical_tokens=1200,
        success_traits=[
            "## Thesis section reframes in strongest form",
            "## Attack Vectors lists >= 5",
            "## Evidence Check prunes weakest",
            "## Mitigations concrete + named",
            "## Verdict = PROMOTE, ITERATE, or KILL with one-line rationale",
        ],
        tier="advanced",
    ),
    Exercise(
        id="BAT-002",
        persona="BATMAN",
        skill=TaskCategory.ADVERSARIAL_REVIEW,
        prompt=(
            "Operator proposes disabling the kill-switch during backtests "
            "to 'get cleaner equity curves'. Adversarial review."
        ),
        typical_tokens=900,
        success_traits=["identifies conflation of backtest vs live", "KILL verdict", "names capital_first doctrine"],
        tier="intermediate",
    ),
    Exercise(
        id="BAT-003",
        persona="BATMAN",
        skill=TaskCategory.GAUNTLET_GATE_DESIGN,
        prompt=(
            "Design a promotion gate ladder for a new intraday setup "
            "targeting MNQ RTH. Input: 30 walk-forward windows. Output: "
            "5-gate ladder with numeric thresholds."
        ),
        typical_tokens=1400,
        success_traits=["5+ gates", "numeric thresholds", "fail-closed defaults", "walk-forward gate present"],
        tier="advanced",
    ),
]


def exercises_for(persona: str, tier: str | None = None) -> list[Exercise]:
    key = persona.upper()
    out = [e for e in EXERCISES if e.persona.upper() == key]
    if tier:
        out = [e for e in out if e.tier == tier]
    return out


def count_per_persona() -> dict[str, int]:
    return {
        persona: sum(1 for e in EXERCISES if e.persona.upper() == persona) for persona in ("BATMAN", "ALFRED", "ROBIN")
    }
