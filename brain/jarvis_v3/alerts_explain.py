"""
JARVIS v3 // alerts_explain
===========================
Why-now traces for alerts.

v2 ``JarvisAlert`` has ``code``, ``message``, ``severity``. v3 adds an
*explanation* trace: the exact components + threshold crossings that
caused the alert to fire, in the order they crossed.

This is the "paper trail" for every CRITICAL fire -- the operator can
reconstruct "what exactly happened at 14:03 ET that made you go RED."

Pure / deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CrossingKind(StrEnum):
    APPROACH = "APPROACH"  # nearing threshold (yellow)
    BREACH = "BREACH"  # crossed threshold (red)
    RECOVER = "RECOVER"  # crossed back below threshold
    HEADLINE = "HEADLINE"  # narrative-level event (FOMC within 1h, etc.)


class ThresholdCrossing(BaseModel):
    """One datum that contributed to an alert."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    factor: str = Field(min_length=1)
    raw_value: float
    threshold: float
    kind: CrossingKind
    note: str = ""


class AlertExplanation(BaseModel):
    """The ``why-now`` for a single alert."""

    model_config = ConfigDict(frozen=True)

    alert_code: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    built_at: datetime
    crossings: list[ThresholdCrossing] = Field(default_factory=list)
    summary: str = ""
    recommendations: list[str] = Field(default_factory=list)


def build_explanation(
    *,
    alert_code: str,
    severity: str,
    contributions: dict[str, float],
    raw_values: dict[str, float],
    thresholds: dict[str, float],
    narrative: str | None = None,
    now: datetime | None = None,
) -> AlertExplanation:
    """Compose an explanation from the alert's contributing factors.

    Parameters
    ----------
    contributions : factor -> weighted contribution (0..1)
    raw_values    : factor -> raw 0..1 value
    thresholds    : factor -> threshold that triggered (approach or breach)
    narrative     : optional higher-level note ("FOMC 12 minutes away")
    """
    now = now or datetime.now(UTC)
    # Sort by contribution descending so the strongest factor comes first.
    ordered = sorted(contributions.items(), key=lambda kv: kv[1], reverse=True)
    crossings: list[ThresholdCrossing] = []
    for factor, _contrib in ordered:
        raw = raw_values.get(factor)
        thr = thresholds.get(factor)
        if raw is None:
            continue
        if thr is not None and raw >= thr:
            kind = CrossingKind.BREACH
            note = f"{raw:.2f} >= threshold {thr:.2f}"
        elif thr is not None and raw >= 0.75 * thr:
            kind = CrossingKind.APPROACH
            note = f"{raw:.2f} ~ threshold {thr:.2f}"
        else:
            # Contribution can still be meaningful even without crossing.
            continue
        crossings.append(
            ThresholdCrossing(
                ts=now,
                factor=factor,
                raw_value=round(raw, 4),
                threshold=round(thr or 0.0, 4),
                kind=kind,
                note=note,
            )
        )
    if narrative:
        crossings.insert(
            0,
            ThresholdCrossing(
                ts=now,
                factor="narrative",
                raw_value=0.0,
                threshold=0.0,
                kind=CrossingKind.HEADLINE,
                note=narrative,
            ),
        )
    if crossings:
        pri = crossings[0]
        summary = (
            f"Primary: {pri.factor} ({pri.kind.value}, {pri.note}); plus {len(crossings) - 1} supporting crossings"
        )
    else:
        summary = "no threshold crossings recorded; alert may be narrative-only"
    recs = _recommendations_for(alert_code, crossings)
    return AlertExplanation(
        alert_code=alert_code,
        severity=severity,
        built_at=now,
        crossings=crossings,
        summary=summary,
        recommendations=recs,
    )


def _recommendations_for(
    alert_code: str,
    crossings: list[ThresholdCrossing],
) -> list[str]:
    out: list[str] = []
    if any(c.factor == "equity_dd" and c.kind == CrossingKind.BREACH for c in crossings):
        out.append("flatten open risk; no new entries until equity_dd recovers below REDUCE")
    if any(c.factor == "macro_event" for c in crossings):
        out.append("stand aside until next macro event resolves")
    if any(c.factor == "override_rate" and c.kind == CrossingKind.BREACH for c in crossings):
        out.append("review last 24h override log; suspend autopilot on breaching subsystem")
    if any(c.factor == "regime_risk" for c in crossings):
        out.append("check regime classifier; may need manual recalibration")
    if not out:
        out.append("monitor; no specific action required yet")
    return out
