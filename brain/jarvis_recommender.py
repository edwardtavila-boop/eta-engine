"""Proactive recommendation engine — JARVIS suggests next moves."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from eta_engine.brain.jarvis_session_state import (
    IterationPhase,
    SessionStateSnapshot,
    SlowBleedLevel,
)


class RecommendationLevel(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class Recommendation(BaseModel):
    level: RecommendationLevel
    code: str = Field(min_length=1)
    title: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    action: str = ""
    lesson_refs: list[int] = Field(default_factory=list)


_LEVEL_ORDER = {
    RecommendationLevel.CRITICAL: 0,
    RecommendationLevel.WARN: 1,
    RecommendationLevel.INFO: 2,
}


def recommend(snap: SessionStateSnapshot) -> list[Recommendation]:
    out: list[Recommendation] = []
    if snap.slow_bleed_level is SlowBleedLevel.TRIPPED:
        out.append(
            Recommendation(
                level=RecommendationLevel.CRITICAL,
                code="slow_bleed_halt",
                title="Halt risk-adding actions — slow-bleed circuit breaker tripped",
                rationale=(
                    f"Rolling expectancy {snap.rolling_expectancy_r:+.4f}R over last "
                    f"{snap.slow_bleed_window_n} trades is below threshold "
                    f"{snap.slow_bleed_threshold_r:+.4f}R."
                ),
                action="Run kill_switch_drift; pause new entries until rolling exp recovers.",
                lesson_refs=[14, 19],
            )
        )
    if snap.gate_auto_fail > 0:
        out.append(
            Recommendation(
                level=RecommendationLevel.CRITICAL,
                code="gate_report_fail_block_promote",
                title=f"{snap.gate_auto_fail} of 5 auto-gates FAIL on {snap.gate_report_label or '(latest)'}",
                rationale="JARVIS will DENY any STRATEGY_PROMOTE_TO_LIVE while auto-gate failures remain.",
                action="Read gate_report JSON; fix data / freeze / regime calibration.",
                lesson_refs=[16, 27],
            )
        )
    if snap.slow_bleed_level is SlowBleedLevel.WARNING:
        out.append(
            Recommendation(
                level=RecommendationLevel.WARN,
                code="slow_bleed_size_cap",
                title="Reduce position size — slow-bleed in WARN zone",
                rationale=f"Rolling expectancy {snap.rolling_expectancy_r:+.4f}R below warn line.",
                action="Continue monitoring. If trips → halt all entries.",
                lesson_refs=[14, 19],
            )
        )
    if snap.trial_budget_alert == "RED" and snap.iteration_phase is IterationPhase.SEARCH:
        out.append(
            Recommendation(
                level=RecommendationLevel.WARN,
                code="trial_budget_red_freeze",
                title=f"Trial budget exhausted ({snap.cumulative_trials} trials) — commit + freeze",
                rationale="DSR is structurally unreachable. Lesson #27: switch to deployment phase.",
                action="`python -m eta_engine.scripts.trial_counter --freeze --label '...'`",
                lesson_refs=[18, 27],
            )
        )
    if snap.gate_report_stale:
        out.append(
            Recommendation(
                level=RecommendationLevel.WARN,
                code="gate_report_stale_rerun",
                title=f"Gate report '{snap.gate_report_label or '(unknown)'}' is stale",
                rationale=f"Age {snap.gate_report_age_hours:.0f}h > threshold "
                f"{snap.gate_report_stale_threshold_hours:.0f}h.",
                action="Re-run `gate_evaluator` to refresh.",
                lesson_refs=[22],
            )
        )
    if snap.regime_label == "choppy":
        out.append(
            Recommendation(
                level=RecommendationLevel.WARN,
                code="regime_choppy_no_entries",
                title=f"Regime CHOPPY (composite {snap.regime_composite:+.3f}) — no new entries",
                rationale="v2.x edge is regime-conditional. Wait for trending classification.",
                action="Monitor regime composite for upward shift.",
                lesson_refs=[28, 29],
            )
        )
    if snap.trial_budget_alert == "YELLOW":
        out.append(
            Recommendation(
                level=RecommendationLevel.INFO,
                code="trial_budget_yellow",
                title=f"Trial budget tight ({snap.trial_budget_remaining} sweeps remaining)",
                rationale="Each additional sweep brings DSR closer to unreachable.",
                action="Prefer high-information sweeps.",
                lesson_refs=[18],
            )
        )
    if snap.iteration_phase is IterationPhase.DEPLOYMENT:
        out.append(
            Recommendation(
                level=RecommendationLevel.INFO,
                code="deployment_phase_active",
                title=f"Iteration FROZEN on '{snap.freeze_label or '(unknown)'}' — paper soak phase",
                rationale="Gate #4 evaluates PSR vs zero. Any sweep will reopen search.",
                action="Continue paper soak.",
                lesson_refs=[27],
            )
        )
    out.sort(key=lambda r: (_LEVEL_ORDER[r.level], r.code))
    return out
