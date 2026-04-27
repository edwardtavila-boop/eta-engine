"""
eta_walkforward.deflated_sharpe
===============================
Deflated Sharpe Ratio (DSR) and Probabilistic Sharpe Ratio (PSR) per
López de Prado, "The Deflated Sharpe Ratio" (2014).

PSR = Phi(  (SR - SR_threshold) * sqrt(n-1) /
            sqrt(1 - skew*SR + (kurtosis-1)/4 * SR^2)  )

DSR = PSR where SR_threshold is set to the expected max SR from N trials
under a null hypothesis (true SR = 0):

  E[max SR] ≈ sqrt(V[SR_null]) * (
      (1 - gamma) * Phi_inv(1 - 1/N) + gamma * Phi_inv(1 - 1/(N*e))
  )

with gamma = 0.5772156649... (Euler-Mascheroni).

All Sharpe inputs are treated as *per-trade* (non-annualized) — the formulas
are scale-invariant across bar frequency as long as SR, skew, and kurtosis
are all from the same return distribution.
"""

from __future__ import annotations

import math

_EULER = 0.5772156649015329
_E = math.e


def _phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _phi_inv(p: float) -> float:
    """Rational approximation to standard normal inverse CDF (Beasley-Springer-Moro)."""
    if p <= 0.0 or p >= 1.0:
        if p <= 0.0:
            return -math.inf
        return math.inf
    # Acklam's algorithm, good to ~1e-9
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


def _expected_max_sr(n_trials: int) -> float:
    """Expected maximum of n_trials iid N(0,1) draws (Gumbel approximation)."""
    if n_trials <= 1:
        return 0.0
    return (
        (1.0 - _EULER) * _phi_inv(1.0 - 1.0 / n_trials)
        + _EULER * _phi_inv(1.0 - 1.0 / (n_trials * _E))
    )


def compute_probabilistic_sharpe(
    sharpe: float,
    threshold: float,
    n_trades: int,
    skew: float,
    kurtosis: float,
) -> float:
    """PSR — probability that true SR exceeds `threshold` given observed stats.

    `kurtosis` is raw kurtosis (3.0 for normal), not excess.
    """
    if n_trades < 2:
        return 0.0
    denom_sq = 1.0 - skew * sharpe + (kurtosis - 1.0) / 4.0 * sharpe * sharpe
    if denom_sq <= 0.0:
        return 0.0
    z = (sharpe - threshold) * math.sqrt(n_trades - 1) / math.sqrt(denom_sq)
    return _phi(z)


def compute_dsr(
    sharpe: float,
    n_trades: int,
    skew: float,
    kurtosis: float,
    n_trials: int = 1,
) -> float:
    """Deflated Sharpe Ratio — PSR with threshold = expected max SR from N trials."""
    threshold = 0.0 if n_trials <= 1 else _expected_max_sr(n_trials)
    return compute_probabilistic_sharpe(sharpe, threshold, n_trades, skew, kurtosis)
