"""
JARVIS v3 // calibration
========================
Platt-calibrated verdict confidence.

v2 verdicts are discrete {APPROVED, CONDITIONAL, DENIED, DEFERRED}. We want
a real-valued probability that the verdict was CORRECT (i.e. if APPROVED,
did the trade work out? if DENIED, did the blocked trade have lost money?).

Approach:
  1. Offline: fit a logistic regression (1-d Platt sigmoid) on a feature
     ``score = f(verdict, stress, binding_constraint, session_phase)``
     -> outcome in {0, 1} pulled from the audit log + journal.
  2. Online: given a fresh verdict, compute ``score`` and pass through
     the fitted sigmoid to get ``p(correct)``.
  3. Store (a, b) sigmoid parameters in a small JSON file so the live
     daemon can read them without re-fitting.

This module provides:
  * ``VerdictFeatures``   -- pydantic feature vector
  * ``PlattSigmoid``      -- fit + predict for a 1-d logistic
  * ``CalibratedVerdict`` -- response wrapper (verdict + p_correct)
  * ``fit_from_audit``    -- loader for the JSONL audit -> fitted sigmoid

Stdlib + pydantic only.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable


class VerdictFeatures(BaseModel):
    """Scalar features consumed by the calibrator."""

    model_config = ConfigDict(frozen=True)

    verdict: str = Field(min_length=1)
    stress_composite: float = Field(ge=0.0, le=1.0)
    sizing_mult: float = Field(ge=0.0, le=1.0, default=1.0)
    session_phase: str = Field(default="OVERNIGHT")
    binding_constraint: str = Field(default="none")
    event_within_1h: bool = Field(default=False)


# Hand-picked coefficients that map a VerdictFeatures -> single scalar "score".
# These are priors used BEFORE any audit data is fit -- the sign encodes
# "higher score = more likely correct verdict". After enough audit samples
# accumulate, PlattSigmoid.fit() overwrites with learned coefficients.
_PRIOR_COEFFS: dict[str, float] = {
    "APPROVED": +1.00,
    "CONDITIONAL": +0.20,
    "DEFERRED": -0.10,
    "DENIED": -0.50,
}


def _linear_score(f: VerdictFeatures) -> float:
    """Map features -> single scalar for the 1-d sigmoid to consume."""
    v_prior = _PRIOR_COEFFS.get(f.verdict.upper(), 0.0)
    score = (
        1.00 * v_prior
        - 1.50 * f.stress_composite  # high stress -> wrong more often
        + 0.50 * f.sizing_mult  # larger approved size -> more decisive
        + (-0.80 if f.event_within_1h else 0.0)  # event-adjacent is noisy
    )
    if f.session_phase in {"OVERNIGHT", "LUNCH"}:
        score -= 0.20
    if f.session_phase in {"OPEN_DRIVE", "MORNING", "AFTERNOON"}:
        score += 0.20
    return score


class PlattSigmoid(BaseModel):
    """Two-parameter logistic sigmoid: p = 1 / (1 + exp(-(a*x + b)))."""

    model_config = ConfigDict(frozen=False)

    a: float = Field(default=1.0)
    b: float = Field(default=0.0)
    fit_samples: int = Field(default=0, ge=0)

    def predict(self, x: float) -> float:
        z = self.a * x + self.b
        # numerically stable logistic
        if z >= 0:
            ez = math.exp(-z)
            return 1.0 / (1.0 + ez)
        ez = math.exp(z)
        return ez / (1.0 + ez)

    def fit(self, xs: list[float], ys: list[int], *, iters: int = 400, lr: float = 0.1) -> None:
        """Mini-batch gradient descent on negative log-likelihood.

        Uses full-batch since we expect small N. Doesn't need numpy.
        """
        if len(xs) != len(ys):
            raise ValueError("xs / ys must be same length")
        if not xs:
            return
        a, b = 0.0, 0.0
        n = len(xs)
        for _ in range(iters):
            grad_a = 0.0
            grad_b = 0.0
            for x, y in zip(xs, ys, strict=True):
                z = a * x + b
                # stable logistic for gradient
                p = 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))
                err = p - y
                grad_a += err * x
                grad_b += err
            a -= lr * grad_a / n
            b -= lr * grad_b / n
        self.a = a
        self.b = b
        self.fit_samples = n


class CalibratedVerdict(BaseModel):
    """Wraps v2 ActionResponse verdict + calibrated probability."""

    model_config = ConfigDict(frozen=True)

    verdict: str = Field(min_length=1)
    p_correct: float = Field(ge=0.0, le=1.0)
    score: float
    fit_samples: int = Field(ge=0, default=0)


def calibrate_verdict(
    features: VerdictFeatures,
    sigmoid: PlattSigmoid | None = None,
) -> CalibratedVerdict:
    """Return a calibrated verdict wrapper. Uses ``sigmoid`` if given,
    otherwise a bootstrapped one-parameter default (slope=1, intercept=0).
    """
    sg = sigmoid or PlattSigmoid(a=1.0, b=0.0)
    score = _linear_score(features)
    p = sg.predict(score)
    return CalibratedVerdict(
        verdict=features.verdict,
        p_correct=round(p, 4),
        score=round(score, 4),
        fit_samples=sg.fit_samples,
    )


def fit_from_audit(
    audit_path: Path | str,
    outcome_key: str = "outcome_correct",
) -> PlattSigmoid:
    """Fit a sigmoid on a JSONL audit log.

    Expected record fields per line:
      * verdict, stress_composite, sizing_mult, session_phase,
        binding_constraint, event_within_1h, outcome_correct (0/1)

    Missing lines / bad JSON are skipped. Returns a fitted sigmoid
    (or a default PlattSigmoid if there are <5 labeled samples).
    """
    path = Path(audit_path)
    xs: list[float] = []
    ys: list[int] = []
    if not path.exists():
        return PlattSigmoid()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if outcome_key not in d:
            continue
        try:
            f = VerdictFeatures(
                verdict=str(d.get("verdict", "")),
                stress_composite=float(d.get("stress_composite", 0.0)),
                sizing_mult=float(d.get("sizing_mult", 1.0)),
                session_phase=str(d.get("session_phase", "OVERNIGHT")),
                binding_constraint=str(d.get("binding_constraint", "none")),
                event_within_1h=bool(d.get("event_within_1h", False)),
            )
        except Exception:  # noqa: BLE001 -- skip malformed records
            continue
        xs.append(_linear_score(f))
        ys.append(1 if int(d[outcome_key]) else 0)
    if len(xs) < 5:
        return PlattSigmoid(a=1.0, b=0.0, fit_samples=len(xs))
    sg = PlattSigmoid()
    sg.fit(xs, ys)
    return sg


def predict_batch(
    features: Iterable[VerdictFeatures],
    sigmoid: PlattSigmoid | None = None,
) -> list[CalibratedVerdict]:
    sg = sigmoid or PlattSigmoid()
    return [calibrate_verdict(f, sg) for f in features]
