"""
JARVIS v3 // critique
=====================
Self-critique / drift detector.

Every N decisions, run a retrospective pass over the audit log + journal
outcomes and answer:

  1. Of the APPROVED requests, how many produced a loss worse than 1R?
     (false-positive rate)
  2. Of the DENIED requests, how many blocked trades that would have been
     winners if taken? (false-negative rate -- reconstructable from
     post-facto market data + simulated fill)
  3. Is the composite stress drifting systematically up or down without
     a corresponding change in outcomes? (policy-drift signal)

Output is a ``CritiqueReport``. If the report trips any RED threshold,
the supervisor fires a CRITICAL alert and JARVIS submits a
``PARAMETER_CHANGE`` request to itself.

Pure / no network. Replay happens from files the caller hands in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class DecisionRecord(BaseModel):
    """Minimum schema the critique engine needs per audit-log line."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    verdict: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    stress_composite: float = Field(ge=0.0, le=1.0)
    # Outcome: 1 = correct, 0 = wrong, None = not yet known.
    outcome_correct: int | None = None
    realized_r: float | None = None  # realized R if it was a trade
    counterfactual_r: float | None = None  # reconstructed R if verdict was DENIED


class CritiqueReport(BaseModel):
    """A retrospective health pass over a window of decisions."""

    model_config = ConfigDict(frozen=True)

    window_start: datetime
    window_end: datetime
    total_decisions: int = Field(ge=0)
    labeled: int = Field(ge=0)
    approved_total: int = Field(ge=0)
    approved_wrong: int = Field(ge=0)
    denied_total: int = Field(ge=0)
    denied_wrong: int = Field(ge=0)
    false_positive_rate: float = Field(ge=0.0, le=1.0)
    false_negative_rate: float = Field(ge=0.0, le=1.0)
    mean_realized_r: float
    mean_missed_r: float
    stress_drift: float = Field(description="Signed mean-delta of composite.")
    severity: str = Field(pattern="^(GREEN|YELLOW|RED)$")
    flags: list[str] = Field(default_factory=list)
    recommendation: str


# Thresholds -- configurable, with safe defaults.
FP_RATE_YELLOW = 0.25
FP_RATE_RED = 0.40
FN_RATE_YELLOW = 0.30
FN_RATE_RED = 0.50
DRIFT_YELLOW = 0.05  # absolute mean-delta on composite
DRIFT_RED = 0.10


def critique_window(
    decisions: list[DecisionRecord],
    *,
    window_days: int = 7,
    now: datetime | None = None,
) -> CritiqueReport:
    """Evaluate the last ``window_days`` of decisions.

    Decisions without ``outcome_correct`` still count toward totals but
    not toward rates. That way the report stays truthful when the
    audit log is mid-flush.
    """
    now = now or datetime.now(UTC)
    window_start = now - timedelta(days=window_days)
    in_window = [d for d in decisions if d.ts >= window_start]
    labeled = [d for d in in_window if d.outcome_correct is not None]
    approved = [d for d in labeled if d.verdict.upper() == "APPROVED"]
    denied = [d for d in labeled if d.verdict.upper() == "DENIED"]
    approved_wrong = sum(1 for d in approved if d.outcome_correct == 0)
    denied_wrong = sum(1 for d in denied if d.outcome_correct == 0)
    fp = approved_wrong / max(1, len(approved))
    fn = denied_wrong / max(1, len(denied))

    realized = [d.realized_r for d in approved if d.realized_r is not None]
    mean_r = sum(realized) / max(1, len(realized))
    missed = [d.counterfactual_r for d in denied if d.counterfactual_r is not None]
    mean_missed = sum(missed) / max(1, len(missed))

    # Stress drift: compare first half vs second half of window.
    if len(in_window) >= 4:
        mid = len(in_window) // 2
        first = sum(d.stress_composite for d in in_window[:mid]) / mid
        second = sum(d.stress_composite for d in in_window[mid:]) / (len(in_window) - mid)
        drift = second - first
    else:
        drift = 0.0

    severity, flags, rec = _classify(fp, fn, drift)

    return CritiqueReport(
        window_start=window_start,
        window_end=now,
        total_decisions=len(in_window),
        labeled=len(labeled),
        approved_total=len(approved),
        approved_wrong=approved_wrong,
        denied_total=len(denied),
        denied_wrong=denied_wrong,
        false_positive_rate=round(fp, 4),
        false_negative_rate=round(fn, 4),
        mean_realized_r=round(mean_r, 4),
        mean_missed_r=round(mean_missed, 4),
        stress_drift=round(drift, 4),
        severity=severity,
        flags=flags,
        recommendation=rec,
    )


def _classify(fp: float, fn: float, drift: float) -> tuple[str, list[str], str]:
    flags: list[str] = []
    sev = "GREEN"
    if fp >= FP_RATE_RED:
        sev = "RED"
        flags.append(f"FP rate {fp:.0%} >= {FP_RATE_RED:.0%}")
    elif fp >= FP_RATE_YELLOW:
        sev = max(sev, "YELLOW", key=["GREEN", "YELLOW", "RED"].index)
        flags.append(f"FP rate {fp:.0%} elevated")
    if fn >= FN_RATE_RED:
        sev = "RED"
        flags.append(f"FN rate {fn:.0%} >= {FN_RATE_RED:.0%}")
    elif fn >= FN_RATE_YELLOW:
        sev = max(sev, "YELLOW", key=["GREEN", "YELLOW", "RED"].index)
        flags.append(f"FN rate {fn:.0%} elevated")
    if abs(drift) >= DRIFT_RED:
        sev = "RED"
        flags.append(f"stress drift {drift:+.3f} >= {DRIFT_RED}")
    elif abs(drift) >= DRIFT_YELLOW:
        sev = max(sev, "YELLOW", key=["GREEN", "YELLOW", "RED"].index)
        flags.append(f"stress drift {drift:+.3f} elevated")

    if sev == "RED":
        rec = "submit PARAMETER_CHANGE to tighten weights; operator review required"
    elif sev == "YELLOW":
        rec = "monitor; consider widening window for more samples"
    else:
        rec = "no action required"
    return sev, flags, rec


def load_decisions(path: Path | str) -> list[DecisionRecord]:
    """Parse a JSONL audit log into DecisionRecord instances; skip bad lines."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[DecisionRecord] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            out.append(DecisionRecord.model_validate(d))
        except Exception:  # noqa: BLE001
            continue
    return out
