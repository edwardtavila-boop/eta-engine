"""Anomaly detection on session_state transitions."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from eta_engine.brain.jarvis_session_state import (
    IterationPhase,
    SessionStateSnapshot,
    SlowBleedLevel,
)


class AnomalySeverity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class Anomaly(BaseModel):
    severity: AnomalySeverity
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    prev_value: str = ""
    curr_value: str = ""


def detect_transition_anomalies(prev: SessionStateSnapshot, curr: SessionStateSnapshot) -> list[Anomaly]:
    out: list[Anomaly] = []
    if prev.slow_bleed_level is SlowBleedLevel.GREEN and curr.slow_bleed_level is SlowBleedLevel.TRIPPED:
        out.append(
            Anomaly(
                severity=AnomalySeverity.CRITICAL,
                code="slow_bleed_skip_warning",
                message="slow_bleed jumped GREEN→TRIPPED in one tick — possible data discontinuity",
                prev_value="GREEN",
                curr_value="TRIPPED",
            )
        )
    if prev.regime_composite is not None and curr.regime_composite is not None:
        delta = abs(curr.regime_composite - prev.regime_composite)
        if delta > 1.0:
            out.append(
                Anomaly(
                    severity=AnomalySeverity.WARN,
                    code="regime_composite_huge_swing",
                    message=f"regime_composite swung {delta:+.3f} in one tick",
                    prev_value=f"{prev.regime_composite:+.3f}",
                    curr_value=f"{curr.regime_composite:+.3f}",
                )
            )
    if curr.cumulative_trials < prev.cumulative_trials:
        same_phase = curr.iteration_phase is prev.iteration_phase
        no_freeze_change = curr.freeze_label == prev.freeze_label
        if same_phase and no_freeze_change:
            out.append(
                Anomaly(
                    severity=AnomalySeverity.CRITICAL,
                    code="cumulative_trials_decreased",
                    message="cumulative_trials decreased without phase / freeze transition",
                    prev_value=str(prev.cumulative_trials),
                    curr_value=str(curr.cumulative_trials),
                )
            )
    if (
        prev.iteration_phase is IterationPhase.DEPLOYMENT
        and curr.iteration_phase is IterationPhase.DEPLOYMENT
        and prev.freeze_label != curr.freeze_label
    ):
        out.append(
            Anomaly(
                severity=AnomalySeverity.WARN,
                code="consecutive_freeze_change",
                message="freeze label changed while remaining in deployment phase",
                prev_value=str(prev.freeze_label),
                curr_value=str(curr.freeze_label),
            )
        )
    if curr.gate_auto_fail > prev.gate_auto_fail:
        out.append(
            Anomaly(
                severity=AnomalySeverity.WARN,
                code="gate_failures_increased",
                message=f"auto-gate failures grew {prev.gate_auto_fail}→{curr.gate_auto_fail}",
                prev_value=str(prev.gate_auto_fail),
                curr_value=str(curr.gate_auto_fail),
            )
        )
    if (
        prev.iteration_phase is IterationPhase.SEARCH
        and curr.iteration_phase is IterationPhase.DEPLOYMENT
        and not curr.freeze_label
    ):
        out.append(
            Anomaly(
                severity=AnomalySeverity.CRITICAL,
                code="phase_flipped_no_label",
                message="phase flipped search→deployment but freeze_label is empty",
                prev_value="search",
                curr_value="deployment (no label)",
            )
        )
    return out
