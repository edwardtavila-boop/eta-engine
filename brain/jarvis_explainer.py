"""
EVOLUTIONARY TRADING ALGO  //  brain.jarvis_explainer
==========================================
JARVIS-as-tutor: given a reason_code from a denial / conditional /
recommendation, return the full markdown context — what it means, why
it fires, which playbook lessons motivate it, what the operator should
do.

Why this exists
---------------
JARVIS produces stable reason codes ("slow_bleed_tripped",
"regime_choppy_no_entries", "gate_report_blocks_promote"). The
operator sees the code in an audit log or recommendation, but has to
hunt for the underlying logic. This module is a single place to ask:
"jarvis explain X" and get a primer.

Public API
----------
  * ``KNOWN_REASON_CODES`` — registry: code → ReasonExplanation
  * ``explain(code)``      — return ReasonExplanation or None
  * ``ReasonExplanation``  — pydantic, with title / details / lessons / actions
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReasonExplanation(BaseModel):
    code: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1, description="One paragraph plain-English")
    triggers_when: str = Field(min_length=1, description="State conditions that produce this code")
    operator_actions: list[str] = Field(default_factory=list)
    lesson_refs: list[int] = Field(default_factory=list)
    related_codes: list[str] = Field(default_factory=list)


KNOWN_REASON_CODES: dict[str, ReasonExplanation] = {
    "slow_bleed_tripped": ReasonExplanation(
        code="slow_bleed_tripped",
        title="Slow-bleed circuit breaker tripped",
        summary=(
            "Rolling per-trade expectancy over the last N trades has fallen "
            "at or below the slow_bleed_threshold_r. JARVIS DENIES every "
            "risk-adding action while the breaker remains tripped. This is "
            "the regime-shift defense the v2.x NQ data justified."
        ),
        triggers_when=(
            "len(recent_trade_rs) >= min_trades_for_check (default 10) AND "
            "mean(recent_trade_rs[-window_n_trades:]) <= expectancy_threshold_r "
            "(default -0.10R)"
        ),
        operator_actions=[
            "Review the last 20 trades in the journal — what changed?",
            "Run `python eta_engine/scripts/_kill_switch_drift.py --hours 24`",
            "If the regime has flipped, halt entries until rolling expectancy recovers above the warn line.",
        ],
        lesson_refs=[14, 19],
        related_codes=["slow_bleed_warning_cap"],
    ),
    "slow_bleed_warning_cap": ReasonExplanation(
        code="slow_bleed_warning_cap",
        title="Slow-bleed in WARN zone — size capped at 50%",
        summary=(
            "Rolling expectancy is between the warn line and the trip "
            "threshold. JARVIS APPROVES risk-adding actions but caps size "
            "at 50% of the live sizing hint. Watch for further deterioration."
        ),
        triggers_when=(
            "rolling_expectancy_r between threshold*warn_ratio (default -0.05R) and threshold (default -0.10R)"
        ),
        operator_actions=[
            "Continue trading at reduced size.",
            "If expectancy worsens to threshold → all entries are denied.",
        ],
        lesson_refs=[14, 19],
        related_codes=["slow_bleed_tripped"],
    ),
    "research_reopens_search": ReasonExplanation(
        code="research_reopens_search",
        title="Research action will reopen search phase (CONDITIONAL)",
        summary=(
            "The trial log is currently FROZEN (committed to a strategy via "
            "freeze marker). Running a parameter sweep or strategy ablation "
            "will append a non-freeze entry that reopens search phase, "
            "reverting gate #4 from PSR (deployment) to DSR (search) basis."
        ),
        triggers_when=(
            "ActionType in {PARAMETER_SWEEP, STRATEGY_ABLATION} AND session_state.iteration_phase == 'deployment'"
        ),
        operator_actions=[
            "If you intend to iterate, set payload['acknowledge_reopen']=True.",
            "After iteration, run `--freeze` again to re-enter deployment phase.",
            "Each reopen cycle adds to the cumulative trial count for the new search.",
        ],
        lesson_refs=[18, 27],
        related_codes=["trial_budget_red_freeze"],
    ),
    "regime_choppy_no_entries": ReasonExplanation(
        code="regime_choppy_no_entries",
        title="Regime classifier reads CHOPPY — entries denied",
        summary=(
            "The composite regime score is below -0.40, indicating the "
            "current bar/window is in a choppy regime where v2.x's edge does "
            "not hold (lesson #28 — regime-conditional knob effects). JARVIS "
            "denies risk-adding actions until the regime classifier returns "
            "to trending or uncertain."
        ),
        triggers_when=("session_state.regime_label == 'choppy' AND ActionType in risk_adding_actions"),
        operator_actions=[
            "Wait for regime composite to climb above -0.40.",
            "If you believe the classifier is misclassifying, recalibrate via `regime_classifier_calibrate.py`.",
        ],
        lesson_refs=[28, 29],
        related_codes=["regime_uncertain_cap"],
    ),
    "regime_uncertain_cap": ReasonExplanation(
        code="regime_uncertain_cap",
        title="Regime UNCERTAIN — size capped at 50%",
        summary=(
            "Composite regime score is between -0.40 and +0.40. Not clearly "
            "trending or choppy. JARVIS approves entries CONDITIONAL with a "
            "size cap at 50% pending clearer regime confirmation."
        ),
        triggers_when=("-0.40 <= session_state.regime_composite <= +0.40 AND ActionType in risk_adding_actions"),
        operator_actions=[
            "Continue trading at reduced size.",
            "Monitor regime composite for resolution.",
        ],
        lesson_refs=[28, 29],
        related_codes=["regime_choppy_no_entries"],
    ),
    "gate_report_blocks_promote": ReasonExplanation(
        code="gate_report_blocks_promote",
        title="Live promotion blocked — gate report has failures",
        summary=(
            "STRATEGY_PROMOTE_TO_LIVE was requested while the latest gate "
            "report shows ≥1 auto-evaluable gate failing or insufficient. "
            "Live capital is denied. The hard policy: no auto-override; use "
            "GATE_OVERRIDE if operator explicitly wants to bypass."
        ),
        triggers_when=("ActionType == STRATEGY_PROMOTE_TO_LIVE AND (gate_auto_fail > 0 OR gate_auto_insufficient > 0)"),
        operator_actions=[
            "Read docs/gate_report_<label>.json to identify failing gate(s).",
            "Most likely fixes: more data (gates 1, 5), iteration freeze "
            "(gate 4), regime detector calibration (gate 6).",
            "If bypassing intentionally: ActionType.GATE_OVERRIDE (operator only).",
        ],
        lesson_refs=[16, 27],
        related_codes=[],
    ),
    "trial_budget_red_freeze": ReasonExplanation(
        code="trial_budget_red_freeze",
        title="Trial budget exhausted — commit + freeze recommended",
        summary=(
            "Cumulative trials have reached the structural ceiling at the "
            "observed Sharpe. Each additional sweep raises the DSR threshold "
            "further from reachable. Lesson #27: switch to deployment phase "
            "via freeze marker — gate #4 then evaluates PSR vs zero (which "
            "IS reachable with more data) instead of DSR (unreachable)."
        ),
        triggers_when=("trial_budget_alert == 'RED' AND iteration_phase == 'search'"),
        operator_actions=[
            "Run `python -m eta_engine.scripts.trial_counter --freeze --label 'commit to <strategy>'`.",
            "After freeze, run paper soak until n_trades clears the "
            "PSR>0.95 threshold (computable via dsr_projection.py).",
        ],
        lesson_refs=[18, 27],
        related_codes=["research_reopens_search"],
    ),
    "gate_report_stale_rerun": ReasonExplanation(
        code="gate_report_stale_rerun",
        title="Gate report is stale — re-run gate evaluator",
        summary=(
            "The latest gate report is older than the staleness threshold "
            "(default 7 days). Promotion decisions need fresh evidence."
        ),
        triggers_when="gate_report_age_hours > stale_threshold_hours (168 default)",
        operator_actions=[
            "Run `python -m eta_engine.scripts.gate_evaluator --overrides <canonical-config> --label <name>`.",
        ],
        lesson_refs=[22],
        related_codes=[],
    ),
}


def explain(code: str) -> ReasonExplanation | None:
    """Return explanation for a reason_code, or None if unknown."""
    return KNOWN_REASON_CODES.get(code)


def render_markdown(exp: ReasonExplanation) -> str:
    """Render a ReasonExplanation as readable markdown for CLI output."""
    lines = [
        f"# {exp.title}",
        f"**Code:** `{exp.code}`",
        "",
        "## Summary",
        exp.summary,
        "",
        "## Triggers when",
        f"```\n{exp.triggers_when}\n```",
        "",
    ]
    if exp.operator_actions:
        lines.append("## What to do")
        for a in exp.operator_actions:
            lines.append(f"- {a}")
        lines.append("")
    if exp.lesson_refs:
        lines.append("## Playbook lessons")
        lines.append(
            "See `docs/strategy_iteration_playbook.md` lessons: " + ", ".join(f"#{n}" for n in exp.lesson_refs)
        )
        lines.append("")
    if exp.related_codes:
        lines.append("## Related codes")
        for c in exp.related_codes:
            lines.append(f"- `{c}`")
    return "\n".join(lines)
