"""Multi-school confluence aggregator.

Aggregates per-school verdicts into a single SageReport with composite
bias + conviction + per-school breakdown. Weight per school is taken
from the school's WEIGHT class attribute.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    SageReport,
    SchoolBase,
    SchoolVerdict,
)


def aggregate(
    verdicts: dict[str, SchoolVerdict],
    schools: dict[str, SchoolBase],
    *,
    entry_side: str = "long",
) -> SageReport:
    """Combine per-school verdicts into a single SageReport.

    Algorithm:
      * each school contributes (weight * conviction) toward its bias direction
      * composite bias = whichever direction (LONG/SHORT) accumulated the most
        weighted conviction; NEUTRAL if both <= a small dead-zone
      * conviction = winner_weight / (winner_weight + loser_weight + neutral_weight)
        clipped to [0, 1]
    """
    if not verdicts:
        return SageReport(
            per_school={},
            composite_bias=Bias.NEUTRAL,
            conviction=0.0,
            schools_consulted=0,
            schools_aligned_with_entry=0,
            schools_disagreeing_with_entry=0,
            schools_neutral=0,
            rationale="no schools consulted",
        )

    long_score = 0.0
    short_score = 0.0
    neutral_score = 0.0

    aligned = 0
    disagreeing = 0
    neutral = 0

    entry_bias = Bias.LONG if entry_side.lower() == "long" else Bias.SHORT

    for name, v in verdicts.items():
        school = schools.get(name)
        weight = school.WEIGHT if school is not None else 1.0
        contrib = weight * v.conviction
        if v.bias == Bias.LONG:
            long_score += contrib
        elif v.bias == Bias.SHORT:
            short_score += contrib
        else:
            neutral_score += contrib

        # Alignment counts use bias != NEUTRAL
        if v.bias == Bias.NEUTRAL:
            neutral += 1
        elif v.bias == entry_bias:
            aligned += 1
        else:
            disagreeing += 1

    total = long_score + short_score + neutral_score

    # Composite bias: dead-zone if both directional scores within 5% of each other
    if max(long_score, short_score) <= 0.10 and neutral_score >= 0.30:
        composite = Bias.NEUTRAL
        winner_score = neutral_score
    elif abs(long_score - short_score) / max(long_score + short_score, 1e-9) < 0.05:
        composite = Bias.NEUTRAL
        winner_score = max(long_score, short_score)
    elif long_score > short_score:
        composite = Bias.LONG
        winner_score = long_score
    elif short_score > long_score:
        composite = Bias.SHORT
        winner_score = short_score
    else:
        composite = Bias.NEUTRAL
        winner_score = neutral_score

    conviction = winner_score / total if total > 0 else 0.0
    conviction = max(0.0, min(1.0, conviction))

    rationale = (
        f"weighted scores: long={long_score:.2f} short={short_score:.2f} "
        f"neutral={neutral_score:.2f} -> composite={composite.value}"
    )

    return SageReport(
        per_school=dict(verdicts),
        composite_bias=composite,
        conviction=conviction,
        schools_consulted=len(verdicts),
        schools_aligned_with_entry=aligned,
        schools_disagreeing_with_entry=disagreeing,
        schools_neutral=neutral,
        rationale=rationale,
    )
