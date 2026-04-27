"""
EVOLUTIONARY TRADING ALGO  //  brain.model_policy
=====================================
Canonical model-tier routing policy (JARVIS-owned).

Why this exists
---------------
Edward's directive (2026-04-19): "Switch the default to Sonnet 4.6 for routine
work. Reserve Opus 4.7 for gnarly architectural decisions (the Firm's Red Team
scoring logic, gauntlet gate design). Haiku 4.5 for grunt work -- log parsing,
simple file edits, commit message drafts. That single swap cuts burn rate ~5x."

Before this module every agent frontmatter defaulted to ``model: opus`` -- the
Max plan was quietly burning 5x quota on tasks that a mid-tier model would do
perfectly. The fix is a *single source of truth* that JARVIS consults whenever
a subsystem (or a sub-agent dispatcher, or a CLAUDE.md directive) needs to pick
the model for a piece of work.

Design principles (mirrors ``brain.jarvis_admin``)
-------------------------------------------------
1. Pure / deterministic. ``select_model(category)`` is a total function.
2. Pydantic-typed. No untyped dicts in the return envelope.
3. StrEnum taxonomies so categories survive JSON round-trip into the audit log.
4. No I/O. The caller decides whether to log the selection.
5. Conservative defaults. An unknown or ambiguous category falls back to
   ``SONNET`` -- the directive's explicit default -- never to OPUS.

Public API
----------
  * ``ModelTier``        -- enum of the three Claude model tiers
  * ``TaskCategory``     -- enum of every task kind the fleet performs
  * ``ModelSelection``   -- pydantic: tier + reason + cost multiplier
  * ``select_model``     -- pure policy (category -> ModelSelection)
  * ``bucket_for``       -- group a TaskCategory into ARCHITECTURAL /
                            ROUTINE / GRUNT (handy for reporting)

Cost multipliers are anchored to SONNET = 1.0x:
  * OPUS   ~= 5.0x  (user directive: "cuts burn rate ~5x" when avoided)
  * SONNET  = 1.0x  (baseline, the new default)
  * HAIKU  ~= 0.2x  (user directive: "literally 1/5 the cost of Sonnet")
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Model tiers
# ---------------------------------------------------------------------------


class ModelTier(StrEnum):
    """The three Claude model tiers the fleet uses.

    Values match what Claude Code expects in sub-agent frontmatter
    (``model: opus`` etc.) so this enum can be written straight to disk.
    """

    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"


# Cost ratios vs. SONNET = 1.0x. Used by reporting / burn-rate dashboards.
# OPUS at ~5.0x is directly from the operator's "5x burn rate" comment;
# HAIKU at ~0.2x is "literally 1/5 the cost of Sonnet".
COST_RATIO: dict[ModelTier, float] = {
    ModelTier.OPUS: 5.0,
    ModelTier.SONNET: 1.0,
    ModelTier.HAIKU: 0.2,
}


# ---------------------------------------------------------------------------
# Task taxonomy
# ---------------------------------------------------------------------------


class TaskBucket(StrEnum):
    """Coarse grouping used for reporting. Each TaskCategory maps to one."""

    ARCHITECTURAL = "architectural"  # -> OPUS
    ROUTINE = "routine"  # -> SONNET (default)
    GRUNT = "grunt"  # -> HAIKU


class TaskCategory(StrEnum):
    """Every task kind the Apex / Firm / mnq_bot fleet performs.

    Keep these stable -- they end up in the audit log and in dashboards.
    New categories must be added to ``_CATEGORY_TO_TIER`` below (enforced
    by ``test_every_category_has_a_tier``).
    """

    # --- ARCHITECTURAL -> OPUS ---------------------------------------------
    # Gnarly design decisions that benefit from Opus's deeper reasoning.
    RED_TEAM_SCORING = "red_team_scoring"  # firm.red_team logic
    GAUNTLET_GATE_DESIGN = "gauntlet_gate_design"  # promotion gates
    RISK_POLICY_DESIGN = "risk_policy_design"  # kill-switch, tiered rollout
    ARCHITECTURE_DECISION = "architecture_decision"  # module layout, boundaries
    ADVERSARIAL_REVIEW = "adversarial_review"  # devil's advocate pass
    STATE_MACHINE_DESIGN = "state_machine_design"  # tiered_rollout, regimes

    # --- ROUTINE -> SONNET (default) ---------------------------------------
    # The bulk of day-to-day development work.
    STRATEGY_EDIT = "strategy_edit"  # confluence / sweep / orb tweaks
    TEST_RUN = "test_run"  # write / run pytest
    REFACTOR = "refactor"  # rename, move, extract
    SKELETON_SCAFFOLD = "skeleton_scaffold"  # new module skeleton, stubs
    CODE_REVIEW = "code_review"  # normal PR review
    DEBUG = "debug"  # fix a failing test / bug
    DOC_WRITING = "doc_writing"  # CLAUDE.md / README updates
    DATA_PIPELINE = "data_pipeline"  # databento / parquet plumbing

    # --- GRUNT -> HAIKU -----------------------------------------------------
    # Mechanical work where a mid-tier model is overkill.
    LOG_PARSING = "log_parsing"  # tail logs / grep / summarize
    SIMPLE_EDIT = "simple_edit"  # rename var, fix typo
    COMMIT_MESSAGE = "commit_message"  # draft commit / PR body
    FORMATTING = "formatting"  # whitespace, imports
    LINT_FIX = "lint_fix"  # ruff / mypy mechanical fixes
    TRIVIAL_LOOKUP = "trivial_lookup"  # find a file / symbol
    BOILERPLATE = "boilerplate"  # __init__.py re-exports


# Single source of truth. Adding a TaskCategory without adding it here will
# trip test_every_category_has_a_tier.
_CATEGORY_TO_TIER: dict[TaskCategory, ModelTier] = {
    # Architectural -> OPUS
    TaskCategory.RED_TEAM_SCORING: ModelTier.OPUS,
    TaskCategory.GAUNTLET_GATE_DESIGN: ModelTier.OPUS,
    TaskCategory.RISK_POLICY_DESIGN: ModelTier.OPUS,
    TaskCategory.ARCHITECTURE_DECISION: ModelTier.OPUS,
    TaskCategory.ADVERSARIAL_REVIEW: ModelTier.OPUS,
    TaskCategory.STATE_MACHINE_DESIGN: ModelTier.OPUS,
    # Routine -> SONNET (default)
    TaskCategory.STRATEGY_EDIT: ModelTier.SONNET,
    TaskCategory.TEST_RUN: ModelTier.SONNET,
    TaskCategory.REFACTOR: ModelTier.SONNET,
    TaskCategory.SKELETON_SCAFFOLD: ModelTier.SONNET,
    TaskCategory.CODE_REVIEW: ModelTier.SONNET,
    TaskCategory.DEBUG: ModelTier.SONNET,
    TaskCategory.DOC_WRITING: ModelTier.SONNET,
    TaskCategory.DATA_PIPELINE: ModelTier.SONNET,
    # Grunt -> HAIKU
    TaskCategory.LOG_PARSING: ModelTier.HAIKU,
    TaskCategory.SIMPLE_EDIT: ModelTier.HAIKU,
    TaskCategory.COMMIT_MESSAGE: ModelTier.HAIKU,
    TaskCategory.FORMATTING: ModelTier.HAIKU,
    TaskCategory.LINT_FIX: ModelTier.HAIKU,
    TaskCategory.TRIVIAL_LOOKUP: ModelTier.HAIKU,
    TaskCategory.BOILERPLATE: ModelTier.HAIKU,
}


_TIER_TO_BUCKET: dict[ModelTier, TaskBucket] = {
    ModelTier.OPUS: TaskBucket.ARCHITECTURAL,
    ModelTier.SONNET: TaskBucket.ROUTINE,
    ModelTier.HAIKU: TaskBucket.GRUNT,
}


# ---------------------------------------------------------------------------
# Selection envelope
# ---------------------------------------------------------------------------


class ModelSelection(BaseModel):
    """What ``select_model`` returns.

    All fields are denormalized so the selection can be written straight
    into an audit log without re-deriving anything.
    """

    model_config = ConfigDict(frozen=True)

    category: TaskCategory
    tier: ModelTier
    bucket: TaskBucket
    cost_multiplier: float = Field(ge=0.0, le=10.0)
    reason: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def select_model(category: TaskCategory) -> ModelSelection:
    """Pure policy: given a task category, pick the model tier.

    The mapping is a single lookup in ``_CATEGORY_TO_TIER``. If the category
    is somehow absent (should be impossible thanks to StrEnum + test coverage)
    we fall back to ``SONNET`` per the operator directive that SONNET is the
    safe default -- never OPUS.
    """
    tier = _CATEGORY_TO_TIER.get(category, ModelTier.SONNET)
    bucket = _TIER_TO_BUCKET[tier]
    reason = _reason_for(category, tier, bucket)
    return ModelSelection(
        category=category,
        tier=tier,
        bucket=bucket,
        cost_multiplier=COST_RATIO[tier],
        reason=reason,
    )


def bucket_for(category: TaskCategory) -> TaskBucket:
    """Return the coarse bucket for a category (for dashboards / reports)."""
    return _TIER_TO_BUCKET[_CATEGORY_TO_TIER[category]]


def tier_for(category: TaskCategory) -> ModelTier:
    """Direct category -> tier lookup (no envelope). Cheap, hot-path friendly."""
    return _CATEGORY_TO_TIER[category]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_BUCKET_BLURB: dict[TaskBucket, str] = {
    TaskBucket.ARCHITECTURAL: ("architectural work -- deeper reasoning justifies the Opus burn"),
    TaskBucket.ROUTINE: ("routine development -- Sonnet is the operator-mandated default"),
    TaskBucket.GRUNT: ("mechanical / grunt work -- Haiku at ~1/5 cost of Sonnet is plenty"),
}


def _reason_for(
    category: TaskCategory,
    tier: ModelTier,
    bucket: TaskBucket,
) -> str:
    return f"{category.value} -> {tier.value} ({_BUCKET_BLURB[bucket]})"
