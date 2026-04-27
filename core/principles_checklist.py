"""
EVOLUTIONARY TRADING ALGO  //  core.principles_checklist
============================================
10-item principles self-audit.

Why this exists
---------------
The user's eta-engine philosophy boils down to ten disciplines. The
checklist is the objective report card -- did you live the principles
this week? The score is mean(yes), 0..1, turned into a letter grade.

Designed to be fed either manually (operator toggles yes/no for each
item in a UI) or programmatically from journal + metric data (so that
items are answered by the system itself wherever possible).

Items (indices fixed; renumbering is a breaking change)
------------------------------------------------------
0. A+ only -- did I pass on B-grade setups?
1. Process over outcome -- did I follow the checklist on every trade?
2. Decision log -- did every trade have a journaled rationale?
3. Financial Jarvis -- did I consult the snapshot before entries?
4. Never on autopilot -- did I ack all watchdog prompts in time?
5. Cadence of review -- did I run the weekly review on schedule?
6. Stress testing -- did I stress-test before any size/parameter change?
7. Risk discipline -- did I stay under daily DD limit?
8. Override discipline -- did I keep override_rate <= 10%?
9. Continuous learning -- did I extract a written lesson from every loser?

Public API
----------
  * ``Principle``            -- one item with question + current yes/no
  * ``ChecklistAnswer``      -- single answered item
  * ``ChecklistReport``      -- all 10 answered + aggregate
  * ``DEFAULT_PRINCIPLES``   -- the canonical 10
  * ``build_report()``       -- given 10 answers -> ChecklistReport
  * ``score_to_letter()``    -- 0..1 -> A+/A/B/C/D/F
"""

from __future__ import annotations

from datetime import UTC, datetime  # noqa: TC003  -- pydantic needs runtime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from collections.abc import Iterable


# ---------------------------------------------------------------------------
# The 10 principles
# ---------------------------------------------------------------------------


class Principle(BaseModel):
    """Metadata for a single principle."""

    index: int = Field(ge=0, le=9)
    slug: str = Field(min_length=1)
    question: str = Field(min_length=1)
    description: str = Field(min_length=1)


DEFAULT_PRINCIPLES: tuple[Principle, ...] = (
    Principle(
        index=0,
        slug="a_plus_only",
        question="Did I pass on B-grade setups?",
        description="Wait for A+ setups; no forcing.",
    ),
    Principle(
        index=1,
        slug="process_over_outcome",
        question="Did I follow my checklist on every trade?",
        description="Grade the process, not the P&L.",
    ),
    Principle(
        index=2,
        slug="decision_log",
        question="Did every trade get a journaled rationale?",
        description="No silent entries; all decisions logged.",
    ),
    Principle(
        index=3,
        slug="consult_jarvis",
        question="Did I consult the Jarvis snapshot before entries?",
        description="Always check context before a new position.",
    ),
    Principle(
        index=4,
        slug="never_autopilot",
        question="Did I ack all watchdog prompts in time?",
        description="Every position, eyes on; no dozing.",
    ),
    Principle(
        index=5,
        slug="cadence_of_review",
        question="Did I run the weekly review on schedule?",
        description="The review is the flywheel.",
    ),
    Principle(
        index=6,
        slug="stress_testing",
        question="Did I stress-test before any size/parameter change?",
        description="No untested knob turns in live.",
    ),
    Principle(
        index=7,
        slug="risk_discipline",
        question="Did I stay under the daily DD limit?",
        description="Risk budget is sacrosanct.",
    ),
    Principle(
        index=8,
        slug="override_discipline",
        question="Did I keep override_rate <= 10%?",
        description="Overrides are expensive; count them.",
    ),
    Principle(
        index=9,
        slug="continuous_learning",
        question="Did I extract a written lesson from every loser?",
        description="Losses paid; lessons owed.",
    ),
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ChecklistAnswer(BaseModel):
    index: int = Field(ge=0, le=9)
    yes: bool
    note: str = Field(default="", max_length=500)

    @field_validator("note")
    @classmethod
    def _strip_note(cls, v: str) -> str:
        return v.strip()


class ChecklistReport(BaseModel):
    ts: datetime
    period_label: str = Field(min_length=1)
    answers: list[ChecklistAnswer]
    score: float = Field(ge=0.0, le=1.0)
    letter_grade: str
    discipline_score: int = Field(
        ge=0,
        le=10,
        description="Number of 'yes' answers out of 10.",
    )
    critical_gaps: list[str] = Field(
        default_factory=list,
        description="Slugs of the failed principles (yes=False).",
    )

    def failed_slugs(self) -> list[str]:
        """Return slugs for answers with yes=False."""
        failed = []
        slug_by_index = {p.index: p.slug for p in DEFAULT_PRINCIPLES}
        for a in self.answers:
            if not a.yes:
                failed.append(slug_by_index[a.index])
        return failed


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_LETTER_BANDS: tuple[tuple[str, float], ...] = (
    ("A+", 0.95),
    ("A", 0.85),
    ("B", 0.75),
    ("C", 0.60),
    ("D", 0.40),
    ("F", 0.0),
)


def score_to_letter(score: float) -> str:
    """Map 0..1 to letter grade. Inclusive lower bounds."""
    if not (0.0 <= score <= 1.0):
        raise ValueError("score must be in [0, 1]")
    for letter, threshold in _LETTER_BANDS:
        if score >= threshold:
            return letter
    return "F"  # unreachable, for linters


def build_report(
    answers: Iterable[ChecklistAnswer],
    *,
    period_label: str,
    ts: datetime | None = None,
) -> ChecklistReport:
    """Aggregate 10 answers into a ChecklistReport.

    Requires exactly one answer per principle index (0..9).
    """
    seen: dict[int, ChecklistAnswer] = {}
    for a in answers:
        if a.index in seen:
            raise ValueError(f"duplicate answer for index {a.index}")
        seen[a.index] = a

    required = {p.index for p in DEFAULT_PRINCIPLES}
    if set(seen.keys()) != required:
        missing = required - set(seen.keys())
        raise ValueError(f"missing answers for indices {sorted(missing)}")

    ordered = [seen[i] for i in sorted(seen.keys())]
    discipline = sum(1 for a in ordered if a.yes)
    score = discipline / 10.0
    letter = score_to_letter(score)

    slug_by_index = {p.index: p.slug for p in DEFAULT_PRINCIPLES}
    critical = [slug_by_index[a.index] for a in ordered if not a.yes]

    return ChecklistReport(
        ts=ts or datetime.now(UTC),
        period_label=period_label,
        answers=ordered,
        score=round(score, 4),
        letter_grade=letter,
        discipline_score=discipline,
        critical_gaps=critical,
    )


def principle_by_index(i: int) -> Principle:
    """Lookup helper."""
    for p in DEFAULT_PRINCIPLES:
        if p.index == i:
            return p
    raise KeyError(f"no principle with index {i}")
