"""
JARVIS v3 // anomaly
====================
Distribution-drift detection on supervisor inputs.

v2 ``jarvis_supervisor`` catches staleness + dominance + flatline on the
OUTPUT composite. This module catches drift on the INPUTS -- VIX jumps,
regime confidence collapse, daily_drawdown distribution shift -- so
JARVIS can flag a bad feed before it corrupts downstream decisions.

Tests:
  1. Z-score spike: a new value > k sigma from rolling mean.
  2. KS-style 2-sample test on two halves of a small window (non-
     parametric, detects shape change).
  3. Constant-feed test: if variance across last N samples is 0 for a
     field that's supposed to change, the feed is likely stuck.

Small/fast -- designed to run every tick.
"""

from __future__ import annotations

import math
import statistics
from collections import deque

from pydantic import BaseModel, ConfigDict, Field


class AnomalyReport(BaseModel):
    """One evaluation of a single field's drift."""

    model_config = ConfigDict(frozen=True)

    field: str = Field(min_length=1)
    severity: str = Field(pattern="^(GREEN|YELLOW|RED)$")
    reason: str = Field(min_length=1)
    z_score: float | None = None
    ks_stat: float | None = None
    samples: int = Field(ge=0)


class DriftDetector:
    """Rolling-window drift tests on a single scalar input field."""

    def __init__(
        self,
        field: str,
        *,
        window: int = 120,
        z_red: float = 4.0,
        z_yellow: float = 2.5,
        ks_yellow: float = 0.25,
        ks_red: float = 0.40,
    ) -> None:
        self.field = field
        self.window = window
        self.z_red = z_red
        self.z_yellow = z_yellow
        self.ks_yellow = ks_yellow
        self.ks_red = ks_red
        self._buf: deque[float] = deque(maxlen=window)

    def observe(self, value: float) -> AnomalyReport:
        self._buf.append(float(value))
        n = len(self._buf)
        if n < 8:
            return AnomalyReport(
                field=self.field,
                severity="GREEN",
                reason=f"warmup ({n}/{self.window})",
                samples=n,
            )
        vals = list(self._buf)
        mu = statistics.fmean(vals[:-1])  # reference stats on PAST (not incl latest)
        sigma = statistics.pstdev(vals[:-1]) if n > 2 else 0.0
        z = (value - mu) / sigma if sigma > 0 else 0.0
        ks = _two_sample_ks(vals)
        # Constant-feed guard: if the last N samples are identical, yellow.
        if _constant(vals):
            return AnomalyReport(
                field=self.field,
                severity="YELLOW",
                reason=f"feed appears stuck at {value:.4f}",
                z_score=0.0,
                ks_stat=0.0,
                samples=n,
            )
        if abs(z) >= self.z_red or ks >= self.ks_red:
            severity = "RED"
            reason = f"z={z:+.2f} ks={ks:.2f} -- distribution shift (mu={mu:.4f}, sigma={sigma:.4f})"
        elif abs(z) >= self.z_yellow or ks >= self.ks_yellow:
            severity = "YELLOW"
            reason = f"elevated z={z:+.2f} ks={ks:.2f}"
        else:
            severity = "GREEN"
            reason = f"stable z={z:+.2f} ks={ks:.2f}"
        return AnomalyReport(
            field=self.field,
            severity=severity,
            reason=reason,
            z_score=round(z, 4),
            ks_stat=round(ks, 4),
            samples=n,
        )


def _two_sample_ks(vals: list[float]) -> float:
    """Simple Kolmogorov-Smirnov 2-sample statistic between two halves."""
    if len(vals) < 4:
        return 0.0
    mid = len(vals) // 2
    a = sorted(vals[:mid])
    b = sorted(vals[mid:])
    all_vals = sorted(set(a) | set(b))

    def _ecdf(sample: list[float], x: float) -> float:
        # position of x in sorted sample / len
        cnt = sum(1 for v in sample if v <= x)
        return cnt / len(sample)

    return max(abs(_ecdf(a, x) - _ecdf(b, x)) for x in all_vals)


def _constant(vals: list[float]) -> bool:
    if len(vals) < 8:
        return False
    s = set(round(v, 8) for v in vals[-8:])
    return len(s) == 1


class MultiFieldDetector:
    """Detect drift across several named fields at once.

    Convenience wrapper for the supervisor -- observe a dict per tick,
    get a list of reports (only elevated severities).
    """

    def __init__(self, fields: list[str], **detector_kwargs: float) -> None:
        self._by_field: dict[str, DriftDetector] = {f: DriftDetector(f, **detector_kwargs) for f in fields}

    def observe(self, payload: dict[str, float]) -> list[AnomalyReport]:
        out: list[AnomalyReport] = []
        for k, v in payload.items():
            if k not in self._by_field:
                continue
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if math.isnan(x) or math.isinf(x):
                out.append(
                    AnomalyReport(
                        field=k,
                        severity="RED",
                        reason="invalid input (NaN/Inf)",
                        samples=0,
                    )
                )
                continue
            report = self._by_field[k].observe(x)
            if report.severity != "GREEN":
                out.append(report)
        return out
