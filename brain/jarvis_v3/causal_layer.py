"""Causal inference layer for JARVIS (Wave-8 #2, 2026-04-27).

Lean port of DoWhy-style causal reasoning using only stdlib + math.
Two primary primitives:

  * GRANGER-style lagged-correlation tests -- "does signal X at t-k
    predict outcome Y at t, beyond what Y's own history predicts?"
    A proxy for causal direction in a stationary system. Cheap and
    interpretable.
  * INTERVENTION lookup via journal -- "in episodes where I DID take
    action A under condition C, what was the realized R distribution
    vs episodes where I did NOT?"

The combined output is a CausalEvidence score in [-1, +1]:
  +1 = strong causal support for the proposed action
   0 = ambiguous / insufficient data
  -1 = strong evidence against (correlations may be spurious)

Use case (pre-trade veto layer):

    from eta_engine.brain.jarvis_v3.causal_layer import score_causal_support

    ev = score_causal_support(
        signal_features={"sentiment": 0.4, "ema_stack": 1, "regime": "bull"},
        proposed_action="approve_full",
        memory=hierarchical_memory,
    )
    if ev.score < -0.3:
        # Past data shows this signal-action combo lacks causal support
        return decision.deny(reason=f"causal_veto: {ev.reason}")

This is a SCAFFOLD. The Granger test is a 1-lag pearson correlation
shortcut; production would use full VAR / regression-residual tests.
The intervention lookup is non-parametric (just bucket comparison)
and assumes the journal is large enough for stable estimates.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

logger = logging.getLogger(__name__)


@dataclass
class CausalEvidence:
    """Output of the causal layer for one (signal, action) pair."""

    score: float                      # in [-1, +1]
    granger_score: float              # in [-1, +1]
    intervention_score: float         # in [-1, +1]
    n_supporting_episodes: int
    reason: str
    detail: dict = field(default_factory=dict)


# ─── Granger-style lagged correlation ───────────────────────────────


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
    if sx == 0 or sy == 0:
        return 0.0
    return cov / (sx * sy)


def granger_score(
    cause_series: Sequence[float],
    outcome_series: Sequence[float],
    *,
    lag: int = 1,
) -> float:
    """Lag-shifted correlation between cause and outcome.

    A 1-lag Pearson on (cause[t-lag], outcome[t]) minus a same-time
    Pearson, capped at [-1, +1]. This is a CHEAP proxy: real Granger
    tests use VAR residuals + F-stat. We return it scaled so the
    output is comparable across features.

    Returns 0 when the series is too short to estimate stably.
    """
    if len(cause_series) < lag + 3 or len(outcome_series) < lag + 3:
        return 0.0
    aligned_cause = list(cause_series[:-lag])
    aligned_outcome = list(outcome_series[lag:])
    lagged_corr = _pearson(aligned_cause, aligned_outcome)
    contemporaneous_corr = _pearson(
        cause_series[lag:], outcome_series[lag:],
    )
    # Difference: how much does adding the lagged term matter?
    raw = lagged_corr - 0.5 * contemporaneous_corr
    return max(-1.0, min(1.0, raw))


# ─── Intervention lookup ────────────────────────────────────────────


def intervention_score(
    *,
    proposed_action: str,
    regime: str,
    session: str,
    direction: str,
    memory: HierarchicalMemory,
    min_episodes: int = 5,
) -> tuple[float, int]:
    """Score how the proposed action has performed under similar
    conditions historically. Returns (score, n_episodes).

    Scoring:
      * If <min_episodes match -> (0.0, n) -- not enough data
      * Otherwise: (avg_r / 2.0) clipped to [-1, +1]
        * +0.5 corresponds to 1R average performance under the
          intervention -- meaningful positive evidence
        * -1.0 corresponds to -2R average (catastrophic)
    """
    similar = memory.recall_similar(
        regime=regime, session=session, stress=0.5,
        direction=direction, k=200,
    )
    matched = [
        e for e in similar
        if e.extra.get("action") == proposed_action
        or proposed_action == "any"
    ]
    if len(matched) < min_episodes:
        return 0.0, len(matched)
    avg_r = sum(e.realized_r for e in matched) / len(matched)
    score = max(-1.0, min(1.0, avg_r / 2.0))
    return score, len(matched)


# ─── Combined evidence ──────────────────────────────────────────────


def score_causal_support(
    *,
    signal_features: dict,
    proposed_action: str,
    regime: str,
    session: str,
    direction: str,
    memory: HierarchicalMemory,
    feature_history: dict | None = None,
) -> CausalEvidence:
    """Combine Granger + intervention into a single causal score.

    ``feature_history`` is optional: a dict of feature_name -> list of
    historical values aligned with the same-length list of historical
    realized_r outcomes. When provided, we Granger-test the most
    important feature (the one with highest |corr| with outcomes)
    against outcomes; otherwise the granger contribution is 0.
    """
    # Granger leg
    granger = 0.0
    if feature_history and "outcome_r" in feature_history:
        outcomes = feature_history["outcome_r"]
        feature_corrs: list[tuple[str, float]] = []
        for name, series in feature_history.items():
            if name == "outcome_r":
                continue
            feature_corrs.append((name, abs(_pearson(series, outcomes))))
        if feature_corrs:
            top_feature, _ = max(feature_corrs, key=lambda t: t[1])
            granger = granger_score(
                feature_history[top_feature], outcomes, lag=1,
            )

    # Intervention leg
    interv, n_ep = intervention_score(
        proposed_action=proposed_action,
        regime=regime, session=session, direction=direction,
        memory=memory,
    )

    # Combined: weighted average; intervention gets more weight since
    # it directly scores the action being proposed.
    combined = 0.4 * granger + 0.6 * interv

    if n_ep < 5:
        reason = (
            f"insufficient episodes (n={n_ep}); falling back on "
            f"granger={granger:+.2f}"
        )
    elif combined > 0.3:
        reason = (
            f"strong causal support ({n_ep} episodes); "
            f"granger={granger:+.2f}, intervention={interv:+.2f}"
        )
    elif combined < -0.3:
        reason = (
            f"causal evidence AGAINST ({n_ep} episodes); "
            f"granger={granger:+.2f}, intervention={interv:+.2f}"
        )
    else:
        reason = (
            f"ambiguous causal evidence ({n_ep} episodes); "
            f"combined={combined:+.2f}"
        )

    return CausalEvidence(
        score=round(combined, 3),
        granger_score=round(granger, 3),
        intervention_score=round(interv, 3),
        n_supporting_episodes=n_ep,
        reason=reason,
        detail={
            "signal_features": signal_features,
            "proposed_action": proposed_action,
        },
    )


# ─── Confounding adjustment helper ──────────────────────────────────


def adjusted_outcome(
    *,
    raw_outcomes: Sequence[float],
    confounders: Sequence[float],
) -> list[float]:
    """Strip out the linear contribution of a confounder.

    Useful when comparing two conditions where stress/regime drift
    might be doing the explanatory work. Returns the residual: raw
    minus best-fit linear model on confounder.

    Pure stdlib; no NumPy. Fits y = a + b*x via OLS.
    """
    n = min(len(raw_outcomes), len(confounders))
    if n < 3:
        return list(raw_outcomes[:n])
    mx = sum(confounders[:n]) / n
    my = sum(raw_outcomes[:n]) / n
    num = sum((confounders[i] - mx) * (raw_outcomes[i] - my) for i in range(n))
    den = sum((confounders[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return list(raw_outcomes[:n])
    b = num / den
    a = my - b * mx
    return [raw_outcomes[i] - (a + b * confounders[i]) for i in range(n)]
