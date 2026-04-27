"""School-dependency graph (Wave-5 #17, 2026-04-27).

Encodes pairwise dependencies: when school X fires with high conviction,
school Y's verdict becomes MORE (or LESS) reliable. Used by the
confluence layer to apply boosts/penalties.

Examples:
  * wyckoff spring + vpa high-vol = both confirmed -> 1.3x boost on each
  * trend_following long + smc_ict ChoCH down = conflict -> 0.5x penalty on weaker
  * stat_significance high + any directional school = boost (signal is real)
"""
from __future__ import annotations

from dataclasses import dataclass

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    SageReport,
    SchoolVerdict,
)


@dataclass(frozen=True)
class DependencyRule:
    """One pairwise rule: when (when_school + when_bias + when_min_conviction)
    fires, apply ``boost`` to ``target_school``'s contribution."""

    when_school: str
    when_bias: Bias
    when_min_conviction: float
    target_school: str
    target_bias: Bias | None  # None = any direction
    boost: float
    name: str


KNOWN_DEPENDENCIES: list[DependencyRule] = [
    # Wyckoff spring + VPA confirms = mutual boost
    DependencyRule(
        name="wyckoff_spring_confirmed_by_vpa",
        when_school="wyckoff", when_bias=Bias.LONG, when_min_conviction=0.7,
        target_school="vpa", target_bias=Bias.LONG, boost=1.3,
    ),
    DependencyRule(
        name="vpa_confirms_wyckoff_long",
        when_school="vpa", when_bias=Bias.LONG, when_min_conviction=0.6,
        target_school="wyckoff", target_bias=Bias.LONG, boost=1.3,
    ),
    # Statistical significance amplifies any directional school
    DependencyRule(
        name="stat_sig_amplifies_long",
        when_school="stat_significance", when_bias=Bias.LONG, when_min_conviction=0.6,
        target_school="trend_following", target_bias=Bias.LONG, boost=1.2,
    ),
    DependencyRule(
        name="stat_sig_amplifies_short",
        when_school="stat_significance", when_bias=Bias.SHORT, when_min_conviction=0.6,
        target_school="trend_following", target_bias=Bias.SHORT, boost=1.2,
    ),
    # Volatility expanding makes order-flow more important
    DependencyRule(
        name="vol_expansion_amplifies_orderflow",
        when_school="volatility_regime", when_bias=Bias.NEUTRAL, when_min_conviction=0.5,
        target_school="order_flow", target_bias=None, boost=1.2,
    ),
    # Red team finds counter -> penalize the trade-side schools
    DependencyRule(
        name="red_team_counter_penalizes_aligned_schools",
        when_school="red_team", when_bias=Bias.SHORT, when_min_conviction=0.6,
        target_school="trend_following", target_bias=Bias.LONG, boost=0.6,
    ),
    DependencyRule(
        name="red_team_counter_penalizes_aligned_schools_short",
        when_school="red_team", when_bias=Bias.LONG, when_min_conviction=0.6,
        target_school="trend_following", target_bias=Bias.SHORT, boost=0.6,
    ),
]


def apply_dependency_boosts(
    verdicts: dict[str, SchoolVerdict],
    *,
    rules: list[DependencyRule] | None = None,
) -> dict[str, float]:
    """Compute per-school weight boosts from the dependency graph.

    Returns ``{school_name: cumulative_boost}``. Schools without
    matching rules return 1.0. Boosts compose multiplicatively when
    multiple rules fire on the same target.
    """
    rules = rules or KNOWN_DEPENDENCIES
    boosts: dict[str, float] = {name: 1.0 for name in verdicts}

    for rule in rules:
        v = verdicts.get(rule.when_school)
        if v is None:
            continue
        if v.bias != rule.when_bias:
            continue
        if v.conviction < rule.when_min_conviction:
            continue
        target_v = verdicts.get(rule.target_school)
        if target_v is None:
            continue
        if rule.target_bias is not None and target_v.bias != rule.target_bias:
            continue
        boosts[rule.target_school] = boosts.get(rule.target_school, 1.0) * rule.boost

    return boosts
