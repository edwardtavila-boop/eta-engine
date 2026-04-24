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
        xs = [rng.gauss(0.0, 0.005) for _ in range(150)] + [
            rng.gauss(0.0, 0.025) for _ in range(150)
        ]
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
        xs = [rng.gauss(0.0, 0.005) for _ in range(500)] + [
            rng.gauss(0.0, 0.025) for _ in range(500)
        ]
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
            variances=[0.01 ** 2],
        )
        assert len(labels) == 1
        assert labels[0] == RegimeType.TRANSITION

    def test_two_states_low_and_high_vol(self) -> None:
        # index 0 has small variance, index 1 has big variance
        labels = map_to_regime_labels(
            means=[0.0, 0.0],
            variances=[0.005 ** 2, 0.030 ** 2],
        )
        assert labels[0] == RegimeType.LOW_VOL
        assert labels[1] == RegimeType.HIGH_VOL

    def test_two_states_high_trend_maps_to_trending(self) -> None:
        # One state has positive drift >> noise; classifier calls it TRENDING
        labels = map_to_regime_labels(
            means=[0.0, 0.005],
            variances=[0.010 ** 2, 0.003 ** 2],
        )
        assert RegimeType.TRENDING in labels

    def test_mismatched_lengths_raise(self) -> None:
        with pytest.raises(ValueError, match="length"):
            map_to_regime_labels(means=[0.0], variances=[0.01, 0.02])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _var(xs: list[float]) -> float:
    n = len(xs)
    m = sum(xs) / n
    return sum((x - m) ** 2 for x in xs) / n


# Appease static-analysis lints for unused imports in this module.
_ = math
