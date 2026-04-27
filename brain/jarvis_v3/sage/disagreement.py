"""School-disagreement intelligence (Wave-5 #5, 2026-04-27).

When two schools disagree directionally, that's not noise -- it's
information. The pair (school_a says LONG, school_b says SHORT) often
maps to a SPECIFIC named market condition with a prescriptive verdict.

Examples:

  dow=LONG + wyckoff=SHORT
    -> "structural uptrend reaching distribution"
    -> verdict: DEFER long entries, watch for upthrust

  trend_following=LONG + smc_ict=SHORT
    -> "trend up but recent ChoCH"
    -> verdict: TIGHTEN cap (likely correction, not reversal)

  vpa=LONG + market_profile=SHORT
    -> "buying pressure into prior value-area high"
    -> verdict: PROCEED but tighten target to VAH

This module exposes two things:
  * KNOWN_PATTERNS -- the curated dict of recognized clash patterns
  * detect_clashes(report) -> list[ClashPattern] -- finds matches in a SageReport
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    SageReport,
    SchoolVerdict,
)


@dataclass(frozen=True)
class ClashPattern:
    """A recognized school-vs-school disagreement pattern."""

    name: str                # e.g. "structural_uptrend_distribution"
    school_a: str
    bias_a: Bias
    school_b: str
    bias_b: Bias
    interpretation: str      # human-readable
    verdict_modifier: str    # "tighten_cap" | "defer" | "loosen_cap" | "no_change"
    cap_mult: float = 1.0    # if verdict_modifier is tighten/loosen, this is the cap


# Curated catalog of known patterns. Add/refine from journal evidence.
# The matching logic in detect_clashes() considers BOTH orderings, so we
# only register each pair once.
KNOWN_PATTERNS: list[ClashPattern] = [
    ClashPattern(
        name="structural_uptrend_topping",
        school_a="dow_theory",      bias_a=Bias.LONG,
        school_b="wyckoff",         bias_b=Bias.SHORT,
        interpretation=(
            "Dow says uptrend but Wyckoff sees distribution -- the structural "
            "trend is intact while smart money is exiting. Often precedes "
            "reversal; long entries should DEFER until Wyckoff turns."
        ),
        verdict_modifier="defer",
    ),
    ClashPattern(
        name="trend_intact_choch_warning",
        school_a="trend_following", bias_a=Bias.LONG,
        school_b="smc_ict",          bias_b=Bias.SHORT,
        interpretation=(
            "Trend bullish but SMC/ICT sees ChoCH down -- likely a correction "
            "in an uptrend, not a reversal. Tighten cap; manage stop tightly."
        ),
        verdict_modifier="tighten_cap",
        cap_mult=0.5,
    ),
    ClashPattern(
        name="momentum_into_value_area_high",
        school_a="vpa",             bias_a=Bias.LONG,
        school_b="market_profile",  bias_b=Bias.SHORT,
        interpretation=(
            "Volume pressure long into a prior value-area high. "
            "Proceed but expect rejection at VAH; tighten profit target."
        ),
        verdict_modifier="tighten_cap",
        cap_mult=0.7,
    ),
    ClashPattern(
        name="dow_long_redteam_short",
        school_a="dow_theory",      bias_a=Bias.LONG,
        school_b="red_team",        bias_b=Bias.SHORT,
        interpretation=(
            "Dow long + adversarial red-team finds a credible short thesis. "
            "Reduce conviction; don't size up."
        ),
        verdict_modifier="tighten_cap",
        cap_mult=0.6,
    ),
    ClashPattern(
        name="risk_violated_anything_long",
        school_a="risk_management", bias_a=Bias.NEUTRAL,  # NEUTRAL + conviction=0 = violation
        school_b="dow_theory",      bias_b=Bias.LONG,
        interpretation=(
            "Risk management school flags non-compliance (cap violated). "
            "DEFER regardless of any other school's direction."
        ),
        verdict_modifier="defer",
    ),
    ClashPattern(
        name="wyckoff_spring_orderflow_disagree",
        school_a="wyckoff",         bias_a=Bias.LONG,
        school_b="order_flow",      bias_b=Bias.SHORT,
        interpretation=(
            "Wyckoff sees spring (long setup) but order flow shows aggressive "
            "selling -- spring may not hold. Reduce size; wait for first "
            "confirming push up before adding."
        ),
        verdict_modifier="tighten_cap",
        cap_mult=0.4,
    ),
    ClashPattern(
        name="vol_regime_quiet_breakout_long",
        school_a="volatility_regime", bias_a=Bias.NEUTRAL,
        school_b="trend_following",   bias_b=Bias.LONG,
        interpretation=(
            "Vol regime quiet + trend says breakout long. Likely "
            "low-conviction; statistically thin vol moves often fade. "
            "Tighten cap."
        ),
        verdict_modifier="tighten_cap",
        cap_mult=0.6,
    ),
]


def detect_clashes(report: SageReport) -> list[ClashPattern]:
    """Scan the SageReport for known disagreement patterns. Returns
    every matching ClashPattern (could be multiple)."""
    matches: list[ClashPattern] = []
    verdicts = report.per_school

    for pat in KNOWN_PATTERNS:
        v_a = verdicts.get(pat.school_a)
        v_b = verdicts.get(pat.school_b)
        if v_a is None or v_b is None:
            continue

        # Match either ordering of (a, b) since the pattern is symmetric.
        match_forward = (v_a.bias == pat.bias_a and v_b.bias == pat.bias_b)
        match_reverse = (v_a.bias == pat.bias_b and v_b.bias == pat.bias_a)

        # Risk-management special-case: NEUTRAL + conviction=0 = violation
        if pat.school_a == "risk_management" and pat.bias_a == Bias.NEUTRAL:
            risk_v = verdicts.get("risk_management")
            if risk_v is not None and risk_v.bias == Bias.NEUTRAL and risk_v.conviction == 0.0:
                if v_b.bias == pat.bias_b:
                    matches.append(pat)
                    continue

        if match_forward or match_reverse:
            matches.append(pat)

    return matches


def strongest_clash_modifier(matches: Iterable[ClashPattern]) -> tuple[str, float]:
    """Reduce a list of matched ClashPatterns into a single (verdict_modifier, cap_mult).

    Order of precedence:
      "defer"        -- always wins (any defer trumps everything)
      "tighten_cap"  -- min cap_mult across all tighten patterns
      "loosen_cap"   -- max cap_mult (if no defer/tighten)
      "no_change"    -- default
    """
    matches = list(matches)
    if not matches:
        return "no_change", 1.0

    if any(m.verdict_modifier == "defer" for m in matches):
        return "defer", 0.0

    tightens = [m.cap_mult for m in matches if m.verdict_modifier == "tighten_cap"]
    if tightens:
        return "tighten_cap", min(tightens)

    loosens = [m.cap_mult for m in matches if m.verdict_modifier == "loosen_cap"]
    if loosens:
        return "loosen_cap", max(loosens)

    return "no_change", 1.0
