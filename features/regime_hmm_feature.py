"""
EVOLUTIONARY TRADING ALGO  //  features.regime_hmm_feature
==============================================
HMM-based regime feature (opt-in, does NOT enter the 5-tuple confluence).

Why an opt-in feature?
----------------------
``features/pipeline.py`` ships a fixed 5-feature confluence tuple
(``trend_bias``, ``vol_regime``, ``funding_skew``, ``onchain_delta``,
``sentiment``) that downstream callers rely on. Adding the HMM
signal to the tuple would silently shift the confluence-score
distribution for every existing consumer. Instead, this feature is
registered explicitly (``pipeline.register(RegimeHMMFeature())``) and
reaches consumers through :meth:`FeaturePipeline.compute_all`. It is
designed to be used as a **research gate or veto**, not a fused
confluence component, until we have dedicated walk-forward validation
of it under live fills.

Why precomputed context?
------------------------
Fitting a Gaussian HMM costs O(K^2 * N) per iteration and the EM
typically needs 10-50 iterations. That's too expensive to do per-bar.
Instead the upstream bot/strategy calls :func:`build_hmm_ctx` on a
rolling window of historical returns (e.g. last 500 5-minute bars),
stores the result on ``ctx["regime_hmm"]``, and refreshes on a slower
cadence (every hour, or every N bars). The feature itself does an
O(K) argmax and a dictionary lookup.

Label-switching safety
----------------------
``build_hmm_ctx`` always passes the fit result through
:func:`brain.regime_hmm.canonicalize_states` so state 0 is the
lowest-variance regime and state K-1 is the most turbulent. Downstream
consumers can key on integer state IDs without seeing phantom regime
changes caused by EM label swaps.

Pipeline boundary
-----------------
``n_states`` defaults to 2 at :func:`build_hmm_ctx` (the pinned value
recommended by the risk-advocate review). Callers who want to pick K
empirically can refit at multiple K offline and compare via
:meth:`HMMFitResult.bic` / :meth:`HMMFitResult.aic`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.brain.regime import RegimeType
from eta_engine.brain.regime_hmm import (
    GaussianHMM,
    canonicalize_states,
    map_to_regime_labels,
)
from eta_engine.features.base import Feature

if TYPE_CHECKING:
    from eta_engine.core.data_pipeline import BarData

# ---------------------------------------------------------------------------
# Score mapping: RegimeType -> [0, 1] confluence score
# ---------------------------------------------------------------------------
#
# Rationale (kept intentionally conservative until we have live fills):
#   TRENDING   -> 1.00  (strong directional signal)
#   LOW_VOL    -> 0.75  (favorable execution conditions)
#   RANGING    -> 0.55  (still tradeable with mean-revert setups)
#   TRANSITION -> 0.50  (no claim either way)
#   HIGH_VOL   -> 0.25  (caution; stops get chopped)
#   CRISIS     -> 0.10  (stand aside)
# ---------------------------------------------------------------------------

_REGIME_SCORE_MAP: dict[RegimeType, float] = {
    RegimeType.TRENDING: 1.00,
    RegimeType.LOW_VOL: 0.75,
    RegimeType.RANGING: 0.55,
    RegimeType.TRANSITION: 0.50,
    RegimeType.HIGH_VOL: 0.25,
    RegimeType.CRISIS: 0.10,
}


class RegimeHMMFeature(Feature):
    """Reads HMM posterior + labels from ctx, emits regime-aware score.

    Expects in ``ctx``:
        ``ctx["regime_hmm"] = {
            "posterior":     [float, ...],         # K probs, sums to 1.0
            "regime_labels": [RegimeType, ...],    # K labels, same length
            "n_states":      int,
        }``

    All error modes -> 0.5 (neutral, does NOT break the default
    pipeline for callers who do not opt into HMM context):

      * missing ``regime_hmm`` key
      * ``regime_hmm`` is ``None``
      * empty ``posterior``
      * ``posterior`` / ``regime_labels`` length mismatch
    """

    name: str = "regime_hmm"
    weight: float = 1.0

    def compute(self, bar: BarData, ctx: dict[str, Any]) -> float:
        payload = ctx.get("regime_hmm") if ctx else None
        if not payload:
            return 0.5
        posterior = payload.get("posterior", []) or []
        labels = payload.get("regime_labels", []) or []
        if not posterior or len(posterior) != len(labels):
            return 0.5

        # Argmax: current most-likely state.
        best_idx = 0
        best_p = posterior[0]
        for i in range(1, len(posterior)):
            if posterior[i] > best_p:
                best_p = posterior[i]
                best_idx = i

        label = labels[best_idx]
        return _REGIME_SCORE_MAP.get(label, 0.5)


# ---------------------------------------------------------------------------
# Helper: fit HMM on a window of returns and build the ctx payload.
# ---------------------------------------------------------------------------


def build_hmm_ctx(
    returns: list[float],
    *,
    n_states: int = 2,
    max_iter: int = 50,
    random_seed: int | None = None,
) -> dict[str, Any]:
    """Fit a Gaussian HMM on ``returns`` and return a ctx-shaped payload.

    Upstream callers do:

        hmm_ctx = build_hmm_ctx(returns=rolling_returns)
        ctx.update(hmm_ctx)                 # now ctx["regime_hmm"] is set
        results = pipeline.compute_all(bar, ctx)

    Parameters
    ----------
    returns
        Per-bar returns window (e.g. last 500 bars).
    n_states
        Number of hidden regimes. Defaults to 2 per the risk-advocate
        review: this is the pipeline boundary for K, and callers that
        want to pick K empirically should call the HMM directly and use
        :meth:`HMMFitResult.bic` offline.
    max_iter, random_seed
        Passed through to :class:`GaussianHMM`.

    Behavior on short input
    -----------------------
    When ``returns`` has fewer than 2 observations the helper returns
    an empty payload (``n_states=0``, empty lists) rather than raising,
    so callers can blindly call it early in a session without gating.
    """
    if len(returns) < 2:
        return {
            "regime_hmm": {
                "posterior": [],
                "regime_labels": [],
                "means": [],
                "variances": [],
                "n_states": 0,
            },
        }

    hmm = GaussianHMM(
        n_states=n_states,
        max_iter=max_iter,
        random_seed=random_seed,
    )
    result = canonicalize_states(hmm.fit(returns))
    posterior_seq = hmm.posterior_probs(returns)
    # Use the LAST-bar posterior as the "current regime" signal. Callers
    # who want the full sequence should call the HMM directly.
    latest_posterior = posterior_seq[-1] if posterior_seq else [1.0] * n_states
    labels = map_to_regime_labels(result.means, result.variances)

    return {
        "regime_hmm": {
            "posterior": list(latest_posterior),
            "regime_labels": list(labels),
            "means": list(result.means),
            "variances": list(result.variances),
            "n_states": n_states,
        },
    }
