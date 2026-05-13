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

_EDGE_TRACKER: object | bool | None = None
_REGIME_MOD_FN: object | bool | None = None


def _get_edge_tracker() -> object | None:
    global _EDGE_TRACKER
    if _EDGE_TRACKER is None:
        try:
            from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker

            _EDGE_TRACKER = default_tracker()  # call to get the singleton instance
        except Exception:  # noqa: BLE001
            _EDGE_TRACKER = False
    return _EDGE_TRACKER if _EDGE_TRACKER is not False else None


def _get_regime_mod_fn() -> object | None:
    global _REGIME_MOD_FN
    if _REGIME_MOD_FN is None:
        try:
            from eta_engine.brain.jarvis_v3.sage.regime import regime_weight_modulator

            _REGIME_MOD_FN = regime_weight_modulator
        except Exception:  # noqa: BLE001
            _REGIME_MOD_FN = False
    return _REGIME_MOD_FN if _REGIME_MOD_FN is not False else None


def aggregate(
    verdicts: dict[str, SchoolVerdict],
    schools: dict[str, SchoolBase],
    *,
    entry_side: str = "long",
    regime: str | None = None,
    apply_edge_weights: bool = True,
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

    # Wave-5 #2 + #3: weight = base * regime_modulator * learned_edge_modifier
    edge_mods: dict[str, float] = {}
    if apply_edge_weights:
        tracker = _get_edge_tracker()
        if tracker is not None:
            edge_mods = tracker.all_weight_modifiers()

    regime_mod_fn = None
    if regime is not None:
        regime_mod_fn = _get_regime_mod_fn()

    # Dependency boosts: when one school signals, correlate-affirming schools
    # get a small bump (e.g. wyckoff spring + dow_theory uptrend = stronger together).
    try:
        from eta_engine.brain.jarvis_v3.sage.dependency_graph import apply_dependency_boosts

        dependency_boosts = apply_dependency_boosts(verdicts)
    except Exception:  # noqa: BLE001
        dependency_boosts = {}

    for name, v in verdicts.items():
        school = schools.get(name)
        base_weight = school.WEIGHT if school is not None else 1.0
        regime_mod = regime_mod_fn(name, regime) if regime_mod_fn else 1.0
        edge_mod = edge_mods.get(name, 1.0)
        dep_boost = dependency_boosts.get(name, 1.0)
        weight = base_weight * regime_mod * edge_mod * dep_boost
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
