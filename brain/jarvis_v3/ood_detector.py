"""Out-of-distribution detector (Wave-13, 2026-04-27).

JARVIS's RAG and world model are both calibrated against the
JOURNAL. When today's market state lies far outside the journal's
distribution, that calibration is no longer valid -- and JARVIS's
confidence should drop accordingly.

The OOD detector quantifies "how unprecedented is this?":

  * Build a low-dimensional feature vector from the current state
    (regime, session, stress, sentiment, slippage, ...)
  * Compare against the journal's feature distribution via
    Mahalanobis-like distance (here: per-feature z-score, summed
    in quadrature -- pure stdlib)
  * Threshold and emit OOD score in [0, 1]

Use case (called from JarvisIntelligence after the firm-board
debate, before final-action synthesis):

    from eta_engine.brain.jarvis_v3.ood_detector import score_ood

    ood = score_ood(
        proposal=proposal, memory=memory,
        recent_features={"sentiment": 0.4, "stress": 0.3, ...},
    )
    if ood.score > 0.7:
        # Very novel -- shrink size, don't trust analog retrieval
        confidence_attenuation = max(0.3, 1.0 - ood.score)

The output is an OodReport with:
  * score: 0 = perfectly typical, 1 = unprecedented
  * label: "typical" / "unusual" / "novel"
  * top_outlier_features: which features are driving the OOD score
  * recommendation: operator-readable advice

Pure stdlib + math. No NumPy.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

logger = logging.getLogger(__name__)


@dataclass
class FeatureOutlier:
    """One feature whose current value sits far from the journal's
    distribution."""

    feature_name: str
    current_value: float
    journal_mean: float
    journal_std: float
    z_score: float


@dataclass
class OodReport:
    """OOD detection summary."""

    score: float  # in [0, 1]
    label: str  # "typical" / "unusual" / "novel"
    n_episodes_compared: int
    top_outlier_features: list[FeatureOutlier] = field(default_factory=list)
    recommendation: str = ""

    def confidence_attenuation(self) -> float:
        """Suggested multiplier for any downstream confidence value.
        1.0 = trust normally, 0.0 = ignore the model entirely."""
        return max(0.0, 1.0 - 0.7 * self.score)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "label": self.label,
            "n_episodes_compared": self.n_episodes_compared,
            "recommendation": self.recommendation,
            "top_outlier_features": [
                {
                    "feature_name": f.feature_name,
                    "current_value": f.current_value,
                    "journal_mean": f.journal_mean,
                    "journal_std": f.journal_std,
                    "z_score": f.z_score,
                }
                for f in self.top_outlier_features
            ],
        }


# ─── Statistics helpers ───────────────────────────────────────────


def _moments(xs: list[float]) -> tuple[float, float]:
    """(mean, std) -- biased sample stdev. Returns (0.0, 0.0) if
    fewer than 2 samples."""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(var)


def _z_score(x: float, mean: float, std: float) -> float:
    """Z-score with fallback for zero-variance journal.

    When the journal has no variance for a feature (e.g. every past
    episode had stress=0.3 exactly) and the current value differs,
    that's MAXIMALLY out-of-distribution -- there's literally no
    precedent. Return a saturating large z-score (~5) in that case.
    """
    if std == 0:
        if abs(x - mean) < 1e-9:
            return 0.0
        return 5.0 if x > mean else -5.0
    return (x - mean) / std


# ─── Main entry point ────────────────────────────────────────────


def score_ood(
    *,
    proposal: Proposal,
    memory: HierarchicalMemory,
    recent_features: dict[str, float] | None = None,
) -> OodReport:
    """Score how OOD the current state is vs the journal.

    The feature set defaults to the structured fields on the
    Proposal (stress, sentiment, sage_score, slippage_bps_estimate);
    callers can pass ``recent_features`` to override or extend with
    their own measurements.

    Returns an OodReport. When the journal has < 5 episodes we return
    score=0 with a warning recommendation -- can't measure novelty
    against an empty distribution.
    """
    if not memory._episodes:
        return OodReport(
            score=0.0,
            label="typical",
            n_episodes_compared=0,
            recommendation=("memory empty; OOD detection inactive (cold-start)"),
        )

    # Default feature set from proposal + extras
    features: dict[str, float] = {
        "stress": float(proposal.stress),
        "sentiment": float(proposal.sentiment),
        "sage_score": float(proposal.sage_score),
        "slippage_bps_estimate": float(proposal.slippage_bps_estimate),
    }
    if recent_features:
        features.update({k: float(v) for k, v in recent_features.items()})

    # Compute per-feature journal moments. We only use episodes whose
    # extra dict carries the same key, OR fall back to known fields
    # (stress -> ep.stress).
    n_episodes = len(memory._episodes)
    outliers: list[FeatureOutlier] = []
    z_squares: list[float] = []

    for fname, cur_val in features.items():
        journal_vals = _extract_feature_series(fname, memory)
        if len(journal_vals) < 5:
            continue
        m, s = _moments(journal_vals)
        z = _z_score(cur_val, m, s)
        z_squares.append(z * z)
        outliers.append(
            FeatureOutlier(
                feature_name=fname,
                current_value=round(cur_val, 4),
                journal_mean=round(m, 4),
                journal_std=round(s, 4),
                z_score=round(z, 3),
            )
        )

    if not z_squares:
        return OodReport(
            score=0.0,
            label="typical",
            n_episodes_compared=n_episodes,
            recommendation="no comparable feature series in journal",
        )

    # Mahalanobis-like aggregation (assumes independence; conservative)
    raw_distance = math.sqrt(sum(z_squares))
    # Convert to a probability-like score in [0, 1]
    # 1 - exp(-d/k) with k tuned so distance ~3 gives ~0.7
    score = 1.0 - math.exp(-raw_distance / 4.0)
    score = max(0.0, min(1.0, score))

    if score < 0.3:
        label = "typical"
        rec = "current state lies within journal distribution; trust models"
    elif score < 0.6:
        label = "unusual"
        rec = "current state in tail of journal distribution; consider shrinking size by 25%"
    else:
        label = "novel"
        rec = (
            "current state OUTSIDE journal distribution; world-model "
            "and RAG retrieval calibration is unreliable -- defer "
            "or shrink size by 50%+"
        )

    # Sort outliers by |z_score| desc; keep top 5
    outliers.sort(key=lambda o: abs(o.z_score), reverse=True)
    return OodReport(
        score=round(score, 3),
        label=label,
        n_episodes_compared=n_episodes,
        top_outlier_features=outliers[:5],
        recommendation=rec,
    )


def _extract_feature_series(
    name: str,
    memory: HierarchicalMemory,
) -> list[float]:
    """Pull one feature's historical values from the journal. Tries
    direct attribute access, then falls back to ``ep.extra[name]``."""
    out: list[float] = []
    for ep in memory._episodes:
        # Try direct attribute (stress, etc.)
        v = getattr(ep, name, None)
        if v is None:
            v = ep.extra.get(name) if ep.extra else None
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out
