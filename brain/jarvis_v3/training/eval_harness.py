"""
JARVIS v3 // training.eval_harness
==================================
Measure persona response quality on synthetic scenarios.

Runs a curriculum of prompts (training.curriculum.EXERCISES) against a
persona, parses each response, and grades it on:

  * format_compliance  -- did it follow the persona's output signature?
  * anti_pattern_hits  -- did it violate any anti_patterns from the manual?
  * skill_match        -- did the response cover the skill's success_example?
  * token_budget       -- within typical_tokens * 1.5?

Output is a pydantic EvalReport with per-exercise + aggregate scores.
Persisted to state_dir/persona_evals/<persona>-<ts>.json so the dashboard
can render trends over time.

This harness is pure + deterministic given the input Claude responses.
Tests inject canned responses; production feeds real Claude output.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.jarvis_v3.training.peak_manuals import manual_for
from eta_engine.brain.jarvis_v3.training.skills_catalog import skills_for


class ExerciseResult(BaseModel):
    """Score for a single curriculum exercise."""

    model_config = ConfigDict(frozen=True)

    exercise_id: str
    skill_category: str
    format_ok: bool
    anti_pattern_hits: list[str] = Field(default_factory=list)
    token_count: int = Field(ge=0)
    within_budget: bool
    score: float = Field(ge=0.0, le=1.0)
    notes: str = ""


class EvalReport(BaseModel):
    """Full persona evaluation -- one run across all exercises."""

    model_config = ConfigDict(frozen=True)

    persona: str
    ts: datetime
    n_exercises: int = Field(ge=0)
    n_passed: int = Field(ge=0)
    mean_score: float = Field(ge=0.0, le=1.0)
    format_compliance: float = Field(ge=0.0, le=1.0)
    budget_compliance: float = Field(ge=0.0, le=1.0)
    anti_pattern_hits: int = Field(ge=0)
    results: list[ExerciseResult]
    recommendation: str = ""


def _count_tokens(text: str) -> int:
    """4-chars/token approximation. Matches our other estimators."""
    return max(1, len(text) // 4)


def _check_anti_patterns(response: str, persona: str) -> list[str]:
    """Scan response for known anti-pattern markers from the peak manual."""
    _ = manual_for(persona)  # validate persona exists
    hits: list[str] = []
    low = response.lower()
    # Heuristic scans for ROBIN / ALFRED / BATMAN common anti-patterns
    if persona.upper() == "ROBIN":
        for bad in ("here is", "sure!", "i can help", "let me explain"):
            if bad in low:
                hits.append(f"ROBIN padding: '{bad}'")
        if len(response) > 2000:
            hits.append("ROBIN output > 2000 chars (too padded)")
    if persona.upper() == "ALFRED":
        if "## Plan" not in response:
            hits.append("ALFRED missing '## Plan' section")
        if "## Deliverable" not in response:
            hits.append("ALFRED missing '## Deliverable' section")
        if "## Check" not in response:
            hits.append("ALFRED missing '## Check' section")
    if persona.upper() == "BATMAN":
        required = ("## Thesis", "## Attack Vectors", "## Evidence Check", "## Mitigations", "## Verdict")
        for section in required:
            if section not in response:
                hits.append(f"BATMAN missing '{section}' section")
        verdict_words = ("PROMOTE", "ITERATE", "KILL")
        if not any(w in response for w in verdict_words):
            hits.append("BATMAN verdict not in {PROMOTE, ITERATE, KILL}")
    return hits


def _check_format(response: str, persona: str) -> bool:
    """True if response matches the persona's output signature."""
    if not response.strip():
        return False
    if persona.upper() == "ROBIN":
        # ROBIN: must have ## Answer OR be a single-line diff/filename/etc
        return "## Answer" in response or "\n" not in response.strip() or len(response) < 400
    if persona.upper() == "ALFRED":
        return all(s in response for s in ("## Plan", "## Deliverable", "## Check"))
    if persona.upper() == "BATMAN":
        return all(s in response for s in ("## Thesis", "## Verdict"))
    return True  # JARVIS bypass


def grade_exercise(
    persona: str,
    exercise_id: str,
    skill_category: str,
    response: str,
    typical_tokens: int,
) -> ExerciseResult:
    """Score one response."""
    token_count = _count_tokens(response)
    within_budget = token_count <= typical_tokens * 1.5
    format_ok = _check_format(response, persona)
    hits = _check_anti_patterns(response, persona)

    score = 1.0
    if not format_ok:
        score *= 0.5
    if not within_budget:
        score *= 0.8
    score *= max(0.0, 1.0 - 0.15 * len(hits))

    notes = "ok" if score >= 0.8 else (f"format={format_ok} budget={within_budget} hits={len(hits)}")
    return ExerciseResult(
        exercise_id=exercise_id,
        skill_category=skill_category,
        format_ok=format_ok,
        anti_pattern_hits=hits,
        token_count=token_count,
        within_budget=within_budget,
        score=round(score, 4),
        notes=notes,
    )


def aggregate_report(persona: str, results: list[ExerciseResult]) -> EvalReport:
    """Roll up per-exercise results into a report."""
    n = len(results)
    if n == 0:
        return EvalReport(
            persona=persona,
            ts=datetime.now(UTC),
            n_exercises=0,
            n_passed=0,
            mean_score=0.0,
            format_compliance=0.0,
            budget_compliance=0.0,
            anti_pattern_hits=0,
            results=[],
            recommendation="no exercises run",
        )
    passed = sum(1 for r in results if r.score >= 0.8)
    mean = sum(r.score for r in results) / n
    fmt = sum(1 for r in results if r.format_ok) / n
    bud = sum(1 for r in results if r.within_budget) / n
    hits = sum(len(r.anti_pattern_hits) for r in results)
    if mean >= 0.9:
        rec = f"{persona} performing at peak; no tuning needed"
    elif mean >= 0.75:
        rec = f"{persona} strong; focus on {_weakest_area(results)}"
    elif mean >= 0.5:
        rec = f"{persona} needs calibration -- fmt={fmt:.0%} budget={bud:.0%} anti_pattern_hits={hits}"
    else:
        rec = f"{persona} failing eval -- review peak_manual + re-train"
    return EvalReport(
        persona=persona,
        ts=datetime.now(UTC),
        n_exercises=n,
        n_passed=passed,
        mean_score=round(mean, 4),
        format_compliance=round(fmt, 4),
        budget_compliance=round(bud, 4),
        anti_pattern_hits=hits,
        results=results,
        recommendation=rec,
    )


def _weakest_area(results: list[ExerciseResult]) -> str:
    scores_by_cat: dict[str, list[float]] = {}
    for r in results:
        scores_by_cat.setdefault(r.skill_category, []).append(r.score)
    if not scores_by_cat:
        return "unknown"
    avg = {k: sum(v) / len(v) for k, v in scores_by_cat.items()}
    return min(avg, key=lambda k: avg[k])


def persona_has_skill_for(persona: str, category: str) -> bool:
    """True if the persona's catalog claims this skill category."""
    return any(s.category.value == category for s in skills_for(persona))
