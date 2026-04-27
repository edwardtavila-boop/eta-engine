"""
EVOLUTIONARY TRADING ALGO  //  brain.regime_hmm
===================================
Gaussian Hidden Markov Model for regime inference from a return series.

Why a second regime classifier?
-------------------------------
:mod:`brain.regime` is a decision-tree classifier on five pre-computed
*axes* (vol, trend, liquidity, correlation, macro). It takes a snapshot
and returns a label.

This module takes a *time series of returns* and jointly learns:

  * per-state mean + variance (the emission distribution)
  * a transition matrix between hidden states
  * an initial-state distribution

and then lets callers:

  * decode the most-likely state sequence (Viterbi),
  * get soft posterior probabilities for every bar,
  * project the learned (mean, variance) states onto the existing
    :class:`~brain.regime.RegimeType` enum via :func:`map_to_regime_labels`.

Together with the decision-tree classifier, you get two independent
regime views: one from *current* features, one from *historical
structure*.

Implementation notes
--------------------
* Pure stdlib ``math``. No numpy dependency, consistent with
  :mod:`backtest.deflated_sharpe` and :mod:`backtest.metrics`.
* Baum-Welch EM with the standard scaled forward-backward algorithm
  (Rabiner 1989). Scaling keeps α / β on the unit simplex each bar so
  likelihoods don't underflow on long series.
* Viterbi decoding runs in log space with a ``_LOG_ZERO`` sentinel for
  zero-probability transitions.
* Variance is floored at ``_VAR_FLOOR`` during updates so a state that
  captures only a handful of near-identical bars doesn't collapse to a
  delta spike (a classic EM failure mode).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from eta_engine.brain.regime import RegimeType

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VAR_FLOOR = 1e-12
_LOG_ZERO = -1e300
_TINY = 1e-300


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class HMMFitResult:
    """Snapshot of a fitted Gaussian HMM.

    Attributes
    ----------
    means, variances
        Per-state Gaussian emission parameters.
    initial_probs
        Marginal distribution over states at ``t=0``. Sums to 1.
    transition_matrix
        Row-stochastic ``K x K`` matrix; ``transition_matrix[i][j]`` is
        ``P(q_{t+1}=j | q_t=i)``.
    log_likelihood_history
        ``log P(O | λ)`` recorded once per EM iteration. Must be
        non-decreasing (Baum-Welch guarantees monotonicity up to
        floating-point noise).
    """

    means: list[float] = field(default_factory=list)
    variances: list[float] = field(default_factory=list)
    initial_probs: list[float] = field(default_factory=list)
    transition_matrix: list[list[float]] = field(default_factory=list)
    log_likelihood_history: list[float] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Model-selection helpers
    # ------------------------------------------------------------------
    def n_parameters(self) -> int:
        """Free-parameter count for BIC/AIC.

        For a K-state Gaussian HMM:

          * K means          (emission)
          * K variances      (emission)
          * (K - 1) initial  (simplex -> one constraint)
          * K * (K - 1) transitions  (each row is a simplex)

        Total = ``2*K^2 + K - 1`` for K >= 1.
        Note: K=1 collapses to ``2`` (mean + variance, no transitions).
        """
        k = len(self.means)
        if k == 0:
            return 0
        if k == 1:
            return 2
        return 2 * k + (k - 1) + k * (k - 1)

    def aic(self) -> float:
        """Akaike Information Criterion on the final-iteration log-likelihood.

        ``AIC = 2k - 2 * log L``. Lower is better. Does NOT scale with
        sample size; use :meth:`bic` if you want a penalty that grows with
        more data.
        """
        if not self.log_likelihood_history:
            raise ValueError("log_likelihood_history is empty; cannot compute AIC")
        return 2.0 * self.n_parameters() - 2.0 * self.log_likelihood_history[-1]

    def bic(self, n_obs: int) -> float:
        """Bayesian Information Criterion on the final-iteration log-likelihood.

        ``BIC = k * ln(n_obs) - 2 * log L``. Lower is better. BIC dominates
        AIC for model selection at realistic sample sizes (e.g. n_obs > ~7
        for any K) because ``ln(n_obs) > 2`` and so the penalty on extra
        parameters is stronger. Use this to pick ``n_states`` on historical
        data: refit at K=1..4, pick the K with the smallest BIC.
        """
        if n_obs <= 0:
            raise ValueError(f"n_obs must be > 0, got {n_obs}")
        if not self.log_likelihood_history:
            raise ValueError("log_likelihood_history is empty; cannot compute BIC")
        return self.n_parameters() * math.log(n_obs) - 2.0 * self.log_likelihood_history[-1]


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class GaussianHMM:
    """Gaussian-emission hidden Markov model fit with Baum-Welch EM.

    Parameters
    ----------
    n_states
        Number of hidden states ``K``. Must be ``>= 1``. ``K=1`` short
        circuits to a single-Gaussian fit (closed-form sample mean + var).
    max_iter
        Maximum number of EM iterations. Default 50.
    tol
        Convergence threshold on the change in log-likelihood between
        iterations. Default ``1e-4``.
    random_seed
        Optional seed for the small perturbations applied during
        parameter initialization. Makes runs reproducible.
    """

    def __init__(
        self,
        n_states: int = 2,
        *,
        max_iter: int = 50,
        tol: float = 1e-4,
        random_seed: int | None = None,
    ) -> None:
        if n_states < 1:
            raise ValueError(f"n_states must be >= 1, got {n_states}")
        if tol <= 0.0:
            raise ValueError(f"tol must be > 0, got {tol}")
        if max_iter < 1:
            raise ValueError(f"max_iter must be >= 1, got {max_iter}")
        self.n_states = n_states
        self.max_iter = max_iter
        self.tol = tol
        self.random_seed = random_seed
        self._result: HMMFitResult | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def fit(self, returns: list[float]) -> HMMFitResult:
        """Fit the HMM to a list of per-bar returns.

        Raises
        ------
        ValueError
            If ``returns`` has fewer than two observations.
        """
        if len(returns) < 2:
            raise ValueError(
                f"returns must have at least 2 observations, got {len(returns)}",
            )

        # K=1 has a closed-form MLE: just the sample mean and variance.
        if self.n_states == 1:
            return self._fit_single_state(returns)

        means, variances, init_probs, trans = self._initialize(returns)
        ll_history: list[float] = []

        for _ in range(self.max_iter):
            # E-step
            alpha, c = _forward_scaled(returns, means, variances, init_probs, trans)
            beta = _backward_scaled(returns, means, variances, trans, c)
            gamma = _posterior_gamma(alpha, beta)
            xi = _posterior_xi(returns, means, variances, trans, alpha, beta)

            # Log-likelihood of the CURRENT parameters (pre-M-step)
            ll = sum(math.log(max(ci, _TINY)) for ci in c)
            ll_history.append(ll)

            # Converged?
            if len(ll_history) >= 2 and abs(ll_history[-1] - ll_history[-2]) < self.tol:
                break

            # M-step ----------------------------------------------------
            init_probs = _m_step_initial(gamma)
            trans = _m_step_transitions(gamma, xi, self.n_states)
            means, variances = _m_step_emissions(returns, gamma, self.n_states)

        # Record final LL after the last M-step (if any).
        _, c_final = _forward_scaled(returns, means, variances, init_probs, trans)
        final_ll = sum(math.log(max(ci, _TINY)) for ci in c_final)
        ll_history.append(final_ll)

        self._result = HMMFitResult(
            means=means,
            variances=variances,
            initial_probs=init_probs,
            transition_matrix=trans,
            log_likelihood_history=ll_history,
        )
        return self._result

    def predict_states(self, returns: list[float]) -> list[int]:
        """Viterbi decode: argmax over state sequences given observations."""
        res = self._require_fit()
        n = len(returns)
        if n == 0:
            return []
        k = self.n_states
        log_init = [_safe_log(p) for p in res.initial_probs]
        log_trans = [[_safe_log(a) for a in row] for row in res.transition_matrix]

        delta = [[0.0] * k for _ in range(n)]
        psi = [[0] * k for _ in range(n)]
        for i in range(k):
            delta[0][i] = log_init[i] + _log_gauss(
                returns[0],
                res.means[i],
                res.variances[i],
            )
        for t in range(1, n):
            obs_log_pdf = [_log_gauss(returns[t], res.means[j], res.variances[j]) for j in range(k)]
            for j in range(k):
                best_i = 0
                best_val = delta[t - 1][0] + log_trans[0][j]
                for i in range(1, k):
                    v = delta[t - 1][i] + log_trans[i][j]
                    if v > best_val:
                        best_val = v
                        best_i = i
                delta[t][j] = best_val + obs_log_pdf[j]
                psi[t][j] = best_i

        # Backtrace
        states = [0] * n
        last = 0
        last_val = delta[n - 1][0]
        for i in range(1, k):
            if delta[n - 1][i] > last_val:
                last_val = delta[n - 1][i]
                last = i
        states[n - 1] = last
        for t in range(n - 2, -1, -1):
            states[t] = psi[t + 1][states[t + 1]]
        return states

    def posterior_probs(self, returns: list[float]) -> list[list[float]]:
        """Forward-backward posterior ``γ_t(i) = P(q_t=i | O, λ)``."""
        res = self._require_fit()
        alpha, c = _forward_scaled(
            returns,
            res.means,
            res.variances,
            res.initial_probs,
            res.transition_matrix,
        )
        beta = _backward_scaled(
            returns,
            res.means,
            res.variances,
            res.transition_matrix,
            c,
        )
        return _posterior_gamma(alpha, beta)

    # ------------------------------------------------------------------
    # Convenience properties mirroring the last fit result
    # ------------------------------------------------------------------
    @property
    def transition_matrix(self) -> list[list[float]]:
        return self._require_fit().transition_matrix

    @property
    def means(self) -> list[float]:
        return self._require_fit().means

    @property
    def variances(self) -> list[float]:
        return self._require_fit().variances

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _require_fit(self) -> HMMFitResult:
        if self._result is None:
            raise RuntimeError("GaussianHMM.fit must be called first")
        return self._result

    def _fit_single_state(self, returns: list[float]) -> HMMFitResult:
        mean = sum(returns) / len(returns)
        var = max(
            sum((x - mean) ** 2 for x in returns) / len(returns),
            _VAR_FLOOR,
        )
        # Two LL entries so callers can still check non-decreasing.
        _, c = _forward_scaled(returns, [mean], [var], [1.0], [[1.0]])
        ll = sum(math.log(max(ci, _TINY)) for ci in c)
        self._result = HMMFitResult(
            means=[mean],
            variances=[var],
            initial_probs=[1.0],
            transition_matrix=[[1.0]],
            log_likelihood_history=[ll, ll],
        )
        return self._result

    def _initialize(
        self,
        returns: list[float],
    ) -> tuple[list[float], list[float], list[float], list[list[float]]]:
        k = self.n_states
        n = len(returns)
        rng = random.Random(self.random_seed)

        sm = sum(returns) / n
        sv = max(sum((x - sm) ** 2 for x in returns) / n, _VAR_FLOOR)

        # Means: evenly-spaced percentiles of the sorted returns so they
        # cover the range. Even if two segments share a mean (pure vol
        # regimes), the percentile spread gives EM a tie-breaker.
        sorted_returns = sorted(returns)
        means: list[float] = []
        for i in range(k):
            p = (2 * i + 1) / (2 * k)
            idx = int(p * n)
            idx = min(max(idx, 0), n - 1)
            means.append(sorted_returns[idx])

        # Variances: fan out 3^i so the low-vol and high-vol states are
        # distinguishable from iteration 0. Without this spread, two
        # identical starting variances leave EM with a symmetric saddle
        # point and the fit can take many iterations to break out.
        variances = [max(sv * (0.25 * (3.0**i)), _VAR_FLOOR) for i in range(k)]

        # Small random perturbation to break any remaining symmetry.
        for i in range(k):
            jitter = 1.0 + 0.1 * (rng.random() - 0.5)
            variances[i] = max(variances[i] * jitter, _VAR_FLOOR)

        init_probs = [1.0 / k] * k
        off = 0.05 / (k - 1) if k > 1 else 0.0
        trans = [[0.95 if i == j else off for j in range(k)] for i in range(k)]
        return means, variances, init_probs, trans


# ---------------------------------------------------------------------------
# Forward-backward helpers (scaled; see Rabiner 1989 §5)
# ---------------------------------------------------------------------------


def _gauss(x: float, mean: float, variance: float) -> float:
    v = max(variance, _VAR_FLOOR)
    return math.exp(-0.5 * (x - mean) ** 2 / v) / math.sqrt(2.0 * math.pi * v)


def _log_gauss(x: float, mean: float, variance: float) -> float:
    v = max(variance, _VAR_FLOOR)
    return -0.5 * (math.log(2.0 * math.pi * v) + (x - mean) ** 2 / v)


def _safe_log(p: float) -> float:
    if p <= 0.0:
        return _LOG_ZERO
    return math.log(p)


def _forward_scaled(
    obs: list[float],
    means: list[float],
    variances: list[float],
    init_probs: list[float],
    trans: list[list[float]],
) -> tuple[list[list[float]], list[float]]:
    n = len(obs)
    k = len(means)
    alpha = [[0.0] * k for _ in range(n)]
    c = [0.0] * n

    # t = 0
    for i in range(k):
        alpha[0][i] = init_probs[i] * _gauss(obs[0], means[i], variances[i])
    s = sum(alpha[0])
    if s <= 0.0:
        c[0] = _TINY
        for i in range(k):
            alpha[0][i] = 1.0 / k
    else:
        c[0] = s
        for i in range(k):
            alpha[0][i] /= s

    # Induction
    for t in range(1, n):
        for j in range(k):
            weight = 0.0
            for i in range(k):
                weight += alpha[t - 1][i] * trans[i][j]
            alpha[t][j] = weight * _gauss(obs[t], means[j], variances[j])
        s = sum(alpha[t])
        if s <= 0.0:
            c[t] = _TINY
            for i in range(k):
                alpha[t][i] = 1.0 / k
        else:
            c[t] = s
            for j in range(k):
                alpha[t][j] /= s
    return alpha, c


def _backward_scaled(
    obs: list[float],
    means: list[float],
    variances: list[float],
    trans: list[list[float]],
    c: list[float],
) -> list[list[float]]:
    n = len(obs)
    k = len(means)
    beta = [[0.0] * k for _ in range(n)]
    denom_last = max(c[n - 1], _TINY)
    for i in range(k):
        beta[n - 1][i] = 1.0 / denom_last
    for t in range(n - 2, -1, -1):
        obs_pdf = [_gauss(obs[t + 1], means[j], variances[j]) for j in range(k)]
        denom = max(c[t], _TINY)
        for i in range(k):
            acc = 0.0
            for j in range(k):
                acc += trans[i][j] * obs_pdf[j] * beta[t + 1][j]
            beta[t][i] = acc / denom
    return beta


def _posterior_gamma(
    alpha: list[list[float]],
    beta: list[list[float]],
) -> list[list[float]]:
    n = len(alpha)
    k = len(alpha[0]) if n else 0
    gamma = [[0.0] * k for _ in range(n)]
    for t in range(n):
        raw = [alpha[t][i] * beta[t][i] for i in range(k)]
        s = sum(raw)
        if s <= 0.0:
            gamma[t] = [1.0 / k] * k
        else:
            gamma[t] = [r / s for r in raw]
    return gamma


def _posterior_xi(
    obs: list[float],
    means: list[float],
    variances: list[float],
    trans: list[list[float]],
    alpha: list[list[float]],
    beta: list[list[float]],
) -> list[list[list[float]]]:
    n = len(obs)
    k = len(means)
    xi = [[[0.0] * k for _ in range(k)] for _ in range(n - 1)]
    for t in range(n - 1):
        obs_pdf = [_gauss(obs[t + 1], means[j], variances[j]) for j in range(k)]
        total = 0.0
        for i in range(k):
            for j in range(k):
                val = alpha[t][i] * trans[i][j] * obs_pdf[j] * beta[t + 1][j]
                xi[t][i][j] = val
                total += val
        if total <= 0.0:
            uniform = 1.0 / (k * k)
            for i in range(k):
                for j in range(k):
                    xi[t][i][j] = uniform
        else:
            for i in range(k):
                for j in range(k):
                    xi[t][i][j] /= total
    return xi


# ---------------------------------------------------------------------------
# M-step helpers
# ---------------------------------------------------------------------------


def _m_step_initial(gamma: list[list[float]]) -> list[float]:
    init = list(gamma[0])
    s = sum(init)
    if s <= 0.0:
        k = len(init)
        return [1.0 / k] * k
    return [p / s for p in init]


def _m_step_transitions(
    gamma: list[list[float]],
    xi: list[list[list[float]]],
    k: int,
) -> list[list[float]]:
    trans = [[0.0] * k for _ in range(k)]
    t_count = len(xi)  # = n-1
    for i in range(k):
        denom = 0.0
        for t in range(t_count):
            denom += gamma[t][i]
        if denom <= _TINY:
            # No mass on state i: leave as near-uniform.
            for j in range(k):
                trans[i][j] = 1.0 / k
            continue
        for j in range(k):
            num = 0.0
            for t in range(t_count):
                num += xi[t][i][j]
            trans[i][j] = num / denom
        # Renormalize row defensively.
        row_sum = sum(trans[i])
        if row_sum > 0.0:
            trans[i] = [a / row_sum for a in trans[i]]
        else:
            trans[i] = [1.0 / k] * k
    return trans


def _m_step_emissions(
    obs: list[float],
    gamma: list[list[float]],
    k: int,
) -> tuple[list[float], list[float]]:
    means = [0.0] * k
    variances = [_VAR_FLOOR] * k
    n = len(obs)
    for j in range(k):
        denom = 0.0
        num = 0.0
        for t in range(n):
            g = gamma[t][j]
            denom += g
            num += g * obs[t]
        if denom <= _TINY:
            # Fallback: sample moments over all observations.
            sm = sum(obs) / n
            means[j] = sm
            variances[j] = max(
                sum((x - sm) ** 2 for x in obs) / n,
                _VAR_FLOOR,
            )
            continue
        mj = num / denom
        means[j] = mj
        var_num = 0.0
        for t in range(n):
            var_num += gamma[t][j] * (obs[t] - mj) ** 2
        variances[j] = max(var_num / denom, _VAR_FLOOR)
    return means, variances


# ---------------------------------------------------------------------------
# Regime label mapper
# ---------------------------------------------------------------------------

_MIN_ABS_DRIFT = 1e-5
"""Absolute-drift floor for TRENDING classification (risk-advocate blocker #2).

On 5m returns, per-bar drift of ``1e-5`` is on the order of the smallest
price tick as a fraction of price. Anything below that is statistical
noise, not a tradeable trend -- regardless of how the model's own sigma
estimate makes the ratio look. Without this floor, a pathologically-tight
low-vol state can appear as TRENDING when both mean and sigma are below
the one-bar-noise scale of the underlying returns.
"""

_MIN_SIGMA_FOR_TRENDING = 1e-5
"""Minimum sigma (not variance) for a state to be eligible for TRENDING.

A state sitting near the variance floor (see ``_VAR_FLOOR = 1e-12``) has
no usable noise estimate; ``|mean|/sigma`` is meaningless. This floor --
one order of magnitude above ``sqrt(_VAR_FLOOR)`` -- keeps the relative
test honest.
"""


def map_to_regime_labels(
    means: list[float],
    variances: list[float],
) -> list[RegimeType]:
    """Project HMM-learned Gaussian states onto :class:`RegimeType`.

    Heuristic (state i evaluated in isolation, then ranked):

      1. A state is ``TRENDING`` iff ALL of:

           * ``|mean_i| > 1e-5`` (absolute drift floor -- see
             ``_MIN_ABS_DRIFT``; prevents the microscopic-mean /
             microscopic-sigma trap)
           * ``sigma_i > 1e-5`` (noise is measurable, not degenerate)
           * ``|mean_i| / sigma_i > 0.5`` (drift dominates one-bar noise)

         Both the absolute and relative conditions must hold.

      2. Among the remaining (non-trending) states:

         * the one with the smallest ``sigma`` becomes ``LOW_VOL``,
         * the one with the largest ``sigma`` becomes ``HIGH_VOL``
           (only if distinct from the ``LOW_VOL`` state),
         * everything in between stays ``TRANSITION``.

      3. A single-state model always collapses to ``TRANSITION`` -- with
         only one regime there is nothing to compare against.

    Raises
    ------
    ValueError
        If ``means`` and ``variances`` are different lengths.
    """
    if len(means) != len(variances):
        raise ValueError(
            f"length: means and variances must be the same length (got {len(means)} vs {len(variances)})",
        )
    k = len(means)
    if k == 0:
        return []
    if k == 1:
        return [RegimeType.TRANSITION]

    vols = [math.sqrt(max(v, 0.0)) for v in variances]
    trend_flags = [
        (
            abs(means[i]) > _MIN_ABS_DRIFT
            and vols[i] > _MIN_SIGMA_FOR_TRENDING
            and abs(means[i]) / max(vols[i], 1e-12) > 0.5
        )
        for i in range(k)
    ]

    labels: list[RegimeType] = [RegimeType.TRANSITION] * k
    for i in range(k):
        if trend_flags[i]:
            labels[i] = RegimeType.TRENDING

    non_trending = [i for i in range(k) if not trend_flags[i]]
    if not non_trending:
        return labels
    if len(non_trending) == 1:
        labels[non_trending[0]] = RegimeType.LOW_VOL
        return labels

    min_i = min(non_trending, key=lambda i: vols[i])
    max_i = max(non_trending, key=lambda i: vols[i])
    labels[min_i] = RegimeType.LOW_VOL
    if max_i != min_i and vols[max_i] > vols[min_i]:
        labels[max_i] = RegimeType.HIGH_VOL
    return labels


# ---------------------------------------------------------------------------
# Canonical state ordering -- risk-advocate blocker #1
# ---------------------------------------------------------------------------


def canonicalize_states(result: HMMFitResult) -> HMMFitResult:
    """Re-order HMM states so state 0 is always the lowest-variance regime.

    Why
    ---
    EM has no intrinsic notion of "state 0" vs "state 1". On a sliding-
    window refit the label-switching symmetry can flip arbitrarily: what
    was state 0 (calm) at time ``t`` can come back as state 1 at ``t+1``.
    Downstream consumers that key on integer state IDs see a sudden
    catastrophic "regime change" that is in fact nothing but a label swap.

    The fix: sort states by variance ascending. State 0 is ALWAYS the
    calmest regime, state K-1 is ALWAYS the most turbulent. The
    transition matrix, initial distribution, and emission parameters are
    all permuted atomically to preserve the joint distribution.

    Notes
    -----
    * ``log_likelihood_history`` is a per-iteration scalar of the whole
      model and is NOT permuted.
    * The input ``result`` is not mutated; a fresh :class:`HMMFitResult`
      is returned.
    """
    k = len(result.variances)
    if k <= 1:
        # Deep-copy-ish clone so callers can't accidentally alias the input.
        return HMMFitResult(
            means=list(result.means),
            variances=list(result.variances),
            initial_probs=list(result.initial_probs),
            transition_matrix=[list(row) for row in result.transition_matrix],
            log_likelihood_history=list(result.log_likelihood_history),
        )

    # Permutation p: new_idx -> old_idx, sorted by variance ascending.
    p = sorted(range(k), key=lambda i: result.variances[i])

    new_means = [result.means[p[i]] for i in range(k)]
    new_variances = [result.variances[p[i]] for i in range(k)]
    new_initial = [result.initial_probs[p[i]] for i in range(k)]
    # Transition matrix needs BOTH axes permuted.
    new_trans = [[result.transition_matrix[p[i]][p[j]] for j in range(k)] for i in range(k)]
    return HMMFitResult(
        means=new_means,
        variances=new_variances,
        initial_probs=new_initial,
        transition_matrix=new_trans,
        log_likelihood_history=list(result.log_likelihood_history),
    )
