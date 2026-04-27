"""tests.test_regime_hmm -- Gaussian HMM regime classifier.

Covers:
  * public API: GaussianHMM.fit / predict_states / posterior_probs /
    transition_matrix / means / variances
  * numerical correctness: single-state degenerate fit, two-state bimodal
    recovery, Viterbi on a known path, transition-probability recovery
  * EM guarantees: log-likelihood non-decreasing across iterations
  * regime mapper: map_to_regime_labels produces RegimeType values
"""

from __future__ import annotations

import math
import random

import pytest

from eta_engine.brain.regime import RegimeType
from eta_engine.brain.regime_hmm import (
    GaussianHMM,
    HMMFitResult,
    canonicalize_states,
    map_to_regime_labels,
)

# ---------------------------------------------------------------------------
# Constructor + shape sanity
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_k_states_at_least_one(self) -> None:
        with pytest.raises(ValueError, match="n_states"):
            GaussianHMM(n_states=0)

    def test_tolerance_positive(self) -> None:
        with pytest.raises(ValueError, match="tol"):
            GaussianHMM(n_states=2, tol=0.0)

    def test_max_iter_positive(self) -> None:
        with pytest.raises(ValueError, match="max_iter"):
            GaussianHMM(n_states=2, max_iter=0)

    def test_defaults_reasonable(self) -> None:
        hmm = GaussianHMM(n_states=2)
        assert hmm.n_states == 2
        assert hmm.max_iter >= 20
        assert 0.0 < hmm.tol < 1.0


# ---------------------------------------------------------------------------
# Fit -- degenerate + bimodal
# ---------------------------------------------------------------------------


class TestFitDegenerate:
    def test_fit_empty_raises(self) -> None:
        hmm = GaussianHMM(n_states=2)
        with pytest.raises(ValueError, match="returns"):
            hmm.fit([])

    def test_fit_too_short_raises(self) -> None:
        hmm = GaussianHMM(n_states=2)
        with pytest.raises(ValueError, match="returns"):
            hmm.fit([0.01])

    def test_fit_single_state_matches_sample_mean(self) -> None:
        rng = random.Random(7)
        xs = [rng.gauss(0.5, 0.1) for _ in range(300)]
        hmm = GaussianHMM(n_states=1, max_iter=50, tol=1e-6)
        result = hmm.fit(xs)
        sample_mean = sum(xs) / len(xs)
        sample_var = sum((x - sample_mean) ** 2 for x in xs) / len(xs)
        assert abs(result.means[0] - sample_mean) < 1e-6
        assert abs(result.variances[0] - sample_var) < 1e-6

    def test_fit_returns_fit_result(self) -> None:
        rng = random.Random(11)
        xs = [rng.gauss(0.0, 0.01) for _ in range(100)]
        hmm = GaussianHMM(n_states=2, max_iter=25)
        result = hmm.fit(xs)
        assert isinstance(result, HMMFitResult)
        assert len(result.means) == 2
        assert len(result.variances) == 2
        assert len(result.initial_probs) == 2
        assert len(result.transition_matrix) == 2
        assert len(result.transition_matrix[0]) == 2
        # All variances are positive
        for v in result.variances:
            assert v > 0.0
        # Stochastic: transition rows sum to 1
        for row in result.transition_matrix:
            assert abs(sum(row) - 1.0) < 1e-6
        # Initial distribution sums to 1
        assert abs(sum(result.initial_probs) - 1.0) < 1e-6


class TestFitBimodal:
    @staticmethod
    def _bimodal(seed: int, n_per_state: int = 200) -> list[float]:
        """Generate a regime-switching series: 200 calm, 200 turbulent, repeat."""
        rng = random.Random(seed)
        out: list[float] = []
        for _ in range(3):
            out.extend(rng.gauss(0.0, 0.005) for _ in range(n_per_state))
            out.extend(rng.gauss(0.0, 0.030) for _ in range(n_per_state))
        return out

    def test_two_means_close_to_zero(self) -> None:
        xs = self._bimodal(seed=31)
        hmm = GaussianHMM(n_states=2, max_iter=80, tol=1e-5, random_seed=2)
        result = hmm.fit(xs)
        # Both means should be near zero since we only changed vol
        for m in result.means:
            assert abs(m) < 0.01

    def test_variances_separate(self) -> None:
        xs = self._bimodal(seed=37)
        hmm = GaussianHMM(n_states=2, max_iter=80, tol=1e-5, random_seed=2)
        result = hmm.fit(xs)
        # One variance should be clearly larger than the other
        low, high = sorted(result.variances)
        assert high > 4.0 * low

    def test_viterbi_separates_calm_from_turbulent(self) -> None:
        xs = self._bimodal(seed=41, n_per_state=250)
        hmm = GaussianHMM(n_states=2, max_iter=80, tol=1e-5, random_seed=3)
        hmm.fit(xs)
        states = hmm.predict_states(xs)
        # Identify which label is "low vol" by its empirical variance
        s0_vals = [x for x, s in zip(xs, states, strict=True) if s == 0]
        s1_vals = [x for x, s in zip(xs, states, strict=True) if s == 1]
        if len(s0_vals) < 2 or len(s1_vals) < 2:
            pytest.fail("HMM collapsed to a single state")
        v0 = _var(s0_vals)
        v1 = _var(s1_vals)
        # The state with larger emp variance should correspond to the
        # actual turbulent segments (0.030^2 >> 0.005^2)
        assert max(v0, v1) > 5.0 * min(v0, v1)


class TestEmMonotonicity:
    def test_log_likelihood_non_decreasing_across_iterations(self) -> None:
        rng = random.Random(61)
        xs = [rng.gauss(0.0, 0.005) for _ in range(150)] + [rng.gauss(0.0, 0.025) for _ in range(150)]
        hmm = GaussianHMM(n_states=2, max_iter=30, tol=1e-12, random_seed=5)
        result = hmm.fit(xs)
        # Accept tiny float drift but reject true decreases
        ll = result.log_likelihood_history
        assert len(ll) >= 2
        for i in range(1, len(ll)):
            assert ll[i] >= ll[i - 1] - 1e-6


# ---------------------------------------------------------------------------
# Transition-matrix recovery
# ---------------------------------------------------------------------------


class TestTransitionMatrixRecovery:
    def test_persistent_states_have_high_self_transition(self) -> None:
        """If the data is two long blocks, A[0][0] and A[1][1] should be large."""
        rng = random.Random(97)
        # 500 calm, then 500 turbulent -- switches exactly once
        xs = [rng.gauss(0.0, 0.005) for _ in range(500)] + [rng.gauss(0.0, 0.025) for _ in range(500)]
        hmm = GaussianHMM(n_states=2, max_iter=60, tol=1e-5, random_seed=7)
        result = hmm.fit(xs)
        # Both self-transition probs should be > 0.95 for near-permanent states
        for i in range(2):
            assert result.transition_matrix[i][i] > 0.95


# ---------------------------------------------------------------------------
# Posterior probabilities -- shape + normalization
# ---------------------------------------------------------------------------


class TestPosteriorProbs:
    def test_rows_sum_to_one(self) -> None:
        rng = random.Random(113)
        xs = [rng.gauss(0.0, 0.01) for _ in range(100)]
        hmm = GaussianHMM(n_states=2, max_iter=25, tol=1e-5, random_seed=1)
        hmm.fit(xs)
        probs = hmm.posterior_probs(xs)
        assert len(probs) == len(xs)
        for row in probs:
            assert len(row) == 2
            assert abs(sum(row) - 1.0) < 1e-6
            for p in row:
                assert 0.0 <= p <= 1.0


# ---------------------------------------------------------------------------
# Regime label mapper
# ---------------------------------------------------------------------------


class TestMapToRegimeLabels:
    def test_single_state_maps_to_transition(self) -> None:
        labels = map_to_regime_labels(
            means=[0.0],
            variances=[0.01**2],
        )
        assert len(labels) == 1
        assert labels[0] == RegimeType.TRANSITION

    def test_two_states_low_and_high_vol(self) -> None:
        # index 0 has small variance, index 1 has big variance
        labels = map_to_regime_labels(
            means=[0.0, 0.0],
            variances=[0.005**2, 0.030**2],
        )
        assert labels[0] == RegimeType.LOW_VOL
        assert labels[1] == RegimeType.HIGH_VOL

    def test_two_states_high_trend_maps_to_trending(self) -> None:
        # One state has positive drift >> noise; classifier calls it TRENDING
        labels = map_to_regime_labels(
            means=[0.0, 0.005],
            variances=[0.010**2, 0.003**2],
        )
        assert RegimeType.TRENDING in labels

    def test_mismatched_lengths_raise(self) -> None:
        with pytest.raises(ValueError, match="length"):
            map_to_regime_labels(means=[0.0], variances=[0.01, 0.02])


# ---------------------------------------------------------------------------
# canonicalize_states -- label-switching defense
# ---------------------------------------------------------------------------


class TestCanonicalizeStates:
    """Risk-advocate blocker #1: EM has no canonical state ordering.

    After refit on a sliding window, state labels can swap arbitrarily.
    `canonicalize_states` sorts by variance ascending so state 0 is
    ALWAYS the lowest-variance ("calm") regime.
    """

    def test_sorts_by_variance_ascending(self) -> None:
        res = HMMFitResult(
            means=[0.01, 0.005, 0.001],
            variances=[0.0009, 0.0001, 0.0004],
            initial_probs=[0.5, 0.3, 0.2],
            transition_matrix=[
                [0.9, 0.05, 0.05],
                [0.1, 0.8, 0.1],
                [0.15, 0.15, 0.7],
            ],
            log_likelihood_history=[100.0, 110.0],
        )
        out = canonicalize_states(res)
        # variances ascending
        assert out.variances == [0.0001, 0.0004, 0.0009]
        # means ride along in permutation order: old 1 -> 0, old 2 -> 1, old 0 -> 2
        assert out.means == [0.005, 0.001, 0.01]
        assert out.initial_probs == [0.3, 0.2, 0.5]
        # LL history is scalar per iteration -- not permuted
        assert out.log_likelihood_history == [100.0, 110.0]

    def test_transition_matrix_permuted_rows_and_cols(self) -> None:
        """T must be reindexed by BOTH axes (rows = from-state, cols = to-state)."""
        # Original vars: [0.2, 0.1, 0.3]. Ascending order of indices: [1, 0, 2].
        res = HMMFitResult(
            means=[1.0, 2.0, 3.0],
            variances=[0.2, 0.1, 0.3],
            initial_probs=[0.1, 0.2, 0.7],
            transition_matrix=[
                [0.7, 0.2, 0.1],
                [0.3, 0.6, 0.1],
                [0.1, 0.1, 0.8],
            ],
            log_likelihood_history=[0.0],
        )
        out = canonicalize_states(res)
        # Permutation p = [1, 0, 2] means new_T[i][j] = old_T[p[i]][p[j]]
        # new_T[0][0] = old_T[1][1] = 0.6
        # new_T[0][1] = old_T[1][0] = 0.3
        # new_T[0][2] = old_T[1][2] = 0.1
        # new_T[1][0] = old_T[0][1] = 0.2
        # new_T[1][1] = old_T[0][0] = 0.7
        # new_T[1][2] = old_T[0][2] = 0.1
        # new_T[2][0] = old_T[2][1] = 0.1
        # new_T[2][1] = old_T[2][0] = 0.1
        # new_T[2][2] = old_T[2][2] = 0.8
        expected = [
            [0.6, 0.3, 0.1],
            [0.2, 0.7, 0.1],
            [0.1, 0.1, 0.8],
        ]
        for i in range(3):
            for j in range(3):
                assert abs(out.transition_matrix[i][j] - expected[i][j]) < 1e-12
        # Rows still sum to 1 (stochasticity preserved)
        for row in out.transition_matrix:
            assert abs(sum(row) - 1.0) < 1e-9

    def test_single_state_is_identity(self) -> None:
        res = HMMFitResult(
            means=[0.001],
            variances=[0.0001],
            initial_probs=[1.0],
            transition_matrix=[[1.0]],
            log_likelihood_history=[42.0],
        )
        out = canonicalize_states(res)
        assert out.means == [0.001]
        assert out.variances == [0.0001]
        assert out.initial_probs == [1.0]
        assert out.transition_matrix == [[1.0]]

    def test_idempotent(self) -> None:
        res = HMMFitResult(
            means=[0.01, 0.005],
            variances=[0.0009, 0.0001],
            initial_probs=[0.5, 0.5],
            transition_matrix=[[0.9, 0.1], [0.1, 0.9]],
            log_likelihood_history=[0.0, 1.0],
        )
        once = canonicalize_states(res)
        twice = canonicalize_states(once)
        assert twice.means == once.means
        assert twice.variances == once.variances
        assert twice.transition_matrix == once.transition_matrix
        assert twice.initial_probs == once.initial_probs

    def test_two_seeds_same_canonical_state_zero(self) -> None:
        """Different seeds on the same data converge to equivalent canonical states.

        Without canonicalization EM may return the low-vol state as index 0
        on one seed and index 1 on another. After `canonicalize_states`,
        state 0 is ALWAYS the lowest-variance regime.
        """
        rng = random.Random(5)
        # Two vol regimes: 250 calm, 250 turbulent, twice
        xs: list[float] = []
        for _ in range(2):
            xs.extend(rng.gauss(0.0, 0.005) for _ in range(250))
            xs.extend(rng.gauss(0.0, 0.030) for _ in range(250))
        hmm_a = GaussianHMM(n_states=2, max_iter=80, tol=1e-5, random_seed=2)
        hmm_b = GaussianHMM(n_states=2, max_iter=80, tol=1e-5, random_seed=99)
        res_a = canonicalize_states(hmm_a.fit(xs))
        res_b = canonicalize_states(hmm_b.fit(xs))
        # State 0 is the lowest-variance state in BOTH fits
        assert res_a.variances[0] <= res_a.variances[1]
        assert res_b.variances[0] <= res_b.variances[1]
        # And they agree on which state it is (same physical regime)
        assert abs(res_a.variances[0] - res_b.variances[0]) / res_a.variances[0] < 0.25

    def test_does_not_mutate_input(self) -> None:
        res = HMMFitResult(
            means=[0.01, 0.001],
            variances=[0.0009, 0.0001],
            initial_probs=[0.5, 0.5],
            transition_matrix=[[0.9, 0.1], [0.1, 0.9]],
            log_likelihood_history=[0.0],
        )
        before_variances = list(res.variances)
        before_trans = [list(row) for row in res.transition_matrix]
        _ = canonicalize_states(res)
        # Input untouched
        assert res.variances == before_variances
        assert res.transition_matrix == before_trans


# ---------------------------------------------------------------------------
# Absolute drift floor -- risk-advocate blocker #2
# ---------------------------------------------------------------------------


class TestMapToRegimeLabelsAbsoluteDriftFloor:
    """On 5m returns, realistic drift is ~1e-4; one-bar noise is ~1e-3.

    The old ``|m|/sigma > 0.5`` test fires on microscopic means that happen
    to sit inside microscopic variance, misclassifying noise as TRENDING.
    The absolute-drift floor of ``|m| > 1e-5`` requires the drift to be
    measurable on the actual return scale, not just relative to the fit's
    own noise estimate.
    """

    def test_microscopic_drift_not_trending(self) -> None:
        # |m|/sigma = 1.0 (very high!) but |m| = 1e-7 (noise-level)
        labels = map_to_regime_labels(
            means=[1e-7, 5e-8],
            variances=[1e-14, 2.5e-15],
        )
        assert RegimeType.TRENDING not in labels

    def test_real_5m_drift_still_trending(self) -> None:
        # mean = 1e-3 (a real bar-level drift), sigma = 2e-3 -> |m|/s = 0.5+
        # Pair it with a calm state for contrast.
        labels = map_to_regime_labels(
            means=[0.0, 0.0015],
            variances=[4e-6, 4e-6],  # sigma = 2e-3
        )
        assert RegimeType.TRENDING in labels

    def test_degenerate_sigma_cannot_trend(self) -> None:
        """A state at or near the variance floor has no trend signal."""
        labels = map_to_regime_labels(
            # state 0: near-delta spike. |m|/sigma may technically clear 0.5
            # but the sigma itself is below a usable floor.
            means=[1e-4, 0.005],
            variances=[1e-13, 1e-4],
        )
        assert labels[0] != RegimeType.TRENDING


# ---------------------------------------------------------------------------
# BIC / AIC -- risk-advocate blocker #3 (model selection)
# ---------------------------------------------------------------------------


class TestBicAic:
    """The pipeline pins ``n_states=2`` but callers need a principled way
    to pick K on historical data. BIC/AIC closed-form from last-iter LL."""

    def test_n_parameters_k1(self) -> None:
        rng = random.Random(41)
        xs = [rng.gauss(0.0, 0.01) for _ in range(100)]
        res = GaussianHMM(n_states=1).fit(xs)
        # K means + K vars + (K-1) init + K*(K-1) trans
        # K=1 -> 1 + 1 + 0 + 0 = 2
        assert res.n_parameters() == 2

    def test_n_parameters_k2(self) -> None:
        rng = random.Random(43)
        xs = [rng.gauss(0.0, 0.01) for _ in range(100)]
        res = GaussianHMM(n_states=2, max_iter=25, random_seed=1).fit(xs)
        # K=2 -> 2 + 2 + 1 + 2 = 7
        assert res.n_parameters() == 7

    def test_n_parameters_k3(self) -> None:
        rng = random.Random(47)
        xs = [rng.gauss(0.0, 0.01) for _ in range(100)]
        res = GaussianHMM(n_states=3, max_iter=25, random_seed=1).fit(xs)
        # K=3 -> 3 + 3 + 2 + 6 = 14
        assert res.n_parameters() == 14

    def test_bic_and_aic_finite(self) -> None:
        rng = random.Random(53)
        xs = [rng.gauss(0.0, 0.01) for _ in range(100)]
        res = GaussianHMM(n_states=1).fit(xs)
        assert math.isfinite(res.bic(n_obs=len(xs)))
        assert math.isfinite(res.aic())

    def test_bic_penalizes_unnecessary_states(self) -> None:
        """On truly single-state data, BIC should prefer K=1 over K=2."""
        rng = random.Random(59)
        xs = [rng.gauss(0.0, 0.01) for _ in range(500)]
        res1 = GaussianHMM(n_states=1).fit(xs)
        res2 = GaussianHMM(n_states=2, max_iter=60, random_seed=3).fit(xs)
        # Lower BIC = better. K=1 should win on homogeneous data.
        assert res1.bic(len(xs)) < res2.bic(len(xs))

    def test_bic_formula_matches_closed_form(self) -> None:
        """BIC = k*ln(n) - 2*LL using the LAST log-likelihood entry."""
        rng = random.Random(61)
        xs = [rng.gauss(0.0, 0.01) for _ in range(200)]
        res = GaussianHMM(n_states=2, max_iter=30, random_seed=1).fit(xs)
        k = res.n_parameters()
        ll = res.log_likelihood_history[-1]
        expected = k * math.log(len(xs)) - 2.0 * ll
        assert abs(res.bic(n_obs=len(xs)) - expected) < 1e-9

    def test_aic_formula_matches_closed_form(self) -> None:
        """AIC = 2k - 2*LL using the LAST log-likelihood entry."""
        rng = random.Random(67)
        xs = [rng.gauss(0.0, 0.01) for _ in range(200)]
        res = GaussianHMM(n_states=2, max_iter=30, random_seed=1).fit(xs)
        k = res.n_parameters()
        ll = res.log_likelihood_history[-1]
        expected = 2.0 * k - 2.0 * ll
        assert abs(res.aic() - expected) < 1e-9

    def test_bic_raises_on_non_positive_n_obs(self) -> None:
        rng = random.Random(71)
        xs = [rng.gauss(0.0, 0.01) for _ in range(50)]
        res = GaussianHMM(n_states=1).fit(xs)
        with pytest.raises(ValueError, match="n_obs"):
            res.bic(n_obs=0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _var(xs: list[float]) -> float:
    n = len(xs)
    m = sum(xs) / n
    return sum((x - m) ** 2 for x in xs) / n


# Appease static-analysis lints for unused imports in this module.
_ = math
