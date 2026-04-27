"""Portfolio VaR / CVaR + correlation brake — P4_SHIELD portfolio_var.

Provides a real-time risk overlay that sits on top of the per-bot risk engine.
Where :mod:`eta_engine.core.risk_engine` asks "is this single trade safe?",
this module asks "is the whole portfolio still safe?"

Offers
------
* :class:`PortfolioRisk` — VaR/CVaR across a returns matrix
* :meth:`PortfolioRisk.correlation_brake` — max pairwise correlation trigger
  that the allocator uses to scale size down when diversification breaks

Inputs are plain numpy arrays so the caller can pull returns from any source —
live positions, backtest PnL, paper-run simulations. No SDK coupling.
"""

from __future__ import annotations

import logging

import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)


class PortfolioRisk:
    """VaR/CVaR + correlation-brake calculator.

    Parameters
    ----------
    confidence_level
        Fraction inside the VaR threshold (0.95 → 95% VaR). Higher = more conservative.
    brake_correlation_threshold
        Max pairwise correlation above which the correlation brake engages.
        Default 0.70 — beyond this, adding more bots buys you no diversification.
    min_observations
        Minimum return samples needed before VaR is considered reliable.
    """

    def __init__(
        self,
        *,
        confidence_level: float = 0.95,
        brake_correlation_threshold: float = 0.70,
        min_observations: int = 20,
    ) -> None:
        if not 0.5 < confidence_level < 1.0:
            raise ValueError(f"confidence_level must be in (0.5, 1.0), got {confidence_level}")
        self.confidence_level = confidence_level
        self.brake_correlation_threshold = brake_correlation_threshold
        self.min_observations = min_observations

    # ── Single-series VaR / CVaR ──

    def var_historical(self, returns: np.ndarray) -> float:
        """Empirical VaR: quantile loss at ``1 - confidence_level``.

        Returns a positive number representing the loss magnitude at the tail.
        For ``confidence_level=0.95`` this is the 5th-percentile loss.
        """
        if len(returns) < self.min_observations:
            logger.warning("var_historical: only %d obs, need %d", len(returns), self.min_observations)
            return 0.0
        q = float(np.quantile(returns, 1.0 - self.confidence_level))
        return float(-q)  # loss-as-positive

    def var_parametric(self, returns: np.ndarray) -> float:
        """Gaussian VaR via μ + zσ. Assumes returns are normally distributed.

        Underestimates tail risk when returns are fat-tailed — use this as a
        cross-check against historical VaR, not as primary.
        """
        if len(returns) < self.min_observations:
            return 0.0
        mu = float(np.mean(returns))
        sigma = float(np.std(returns, ddof=1))
        z = float(stats.norm.ppf(1.0 - self.confidence_level))
        return float(-(mu + z * sigma))

    def cvar(self, returns: np.ndarray) -> float:
        """Conditional VaR (Expected Shortfall): average loss beyond VaR.

        Always >= var_historical. Used when tail-risk policy is 'expected bad
        day' rather than 'bad day threshold'.
        """
        if len(returns) < self.min_observations:
            return 0.0
        cutoff = np.quantile(returns, 1.0 - self.confidence_level)
        tail = returns[returns <= cutoff]
        if len(tail) == 0:
            return 0.0
        return float(-np.mean(tail))

    # ── Portfolio-level ──

    def portfolio_var(
        self,
        returns_matrix: np.ndarray,
        weights: np.ndarray | None = None,
    ) -> float:
        """VaR of a weighted portfolio of bots.

        ``returns_matrix`` is (T, N) — T samples, N bots.
        ``weights`` sums to 1 across N bots. Default is equal-weight.
        """
        if returns_matrix.ndim != 2:
            raise ValueError(f"returns_matrix must be 2D, got shape {returns_matrix.shape}")
        n_bots = returns_matrix.shape[1]
        if weights is None:
            weights = np.ones(n_bots) / n_bots
        if len(weights) != n_bots:
            raise ValueError(f"weights length {len(weights)} != bot count {n_bots}")
        if not np.isclose(weights.sum(), 1.0, atol=1e-4):
            raise ValueError(f"weights must sum to 1.0, got {weights.sum():.4f}")
        portfolio_returns = returns_matrix @ weights
        return self.var_historical(portfolio_returns)

    # ── Correlation Brake ──

    def correlation_brake(self, returns_matrix: np.ndarray) -> dict[str, float | bool]:
        """Check if max pairwise correlation exceeds brake threshold.

        Returns a structured verdict the allocator can consume:
            {
                "max_correlation": 0.82,
                "brake_engaged": True,
                "correlation_matrix_mean_offdiag": 0.67,
                "pairs_above_threshold": 3,
            }
        """
        if returns_matrix.ndim != 2 or returns_matrix.shape[1] < 2:
            return {
                "max_correlation": 0.0,
                "brake_engaged": False,
                "correlation_matrix_mean_offdiag": 0.0,
                "pairs_above_threshold": 0,
            }
        corr = np.corrcoef(returns_matrix, rowvar=False)
        # Mask diagonal so self-corr doesn't dominate.
        n = corr.shape[0]
        mask = ~np.eye(n, dtype=bool)
        off_diag = corr[mask]
        max_corr = float(np.nanmax(off_diag))
        mean_corr = float(np.nanmean(off_diag))
        pairs_above = int(np.sum(off_diag > self.brake_correlation_threshold) / 2)  # matrix is symmetric
        engaged = max_corr > self.brake_correlation_threshold
        if engaged:
            logger.warning(
                "correlation brake engaged: max=%.3f mean_offdiag=%.3f pairs_above=%d",
                max_corr,
                mean_corr,
                pairs_above,
            )
        return {
            "max_correlation": max_corr,
            "brake_engaged": engaged,
            "correlation_matrix_mean_offdiag": mean_corr,
            "pairs_above_threshold": pairs_above,
        }

    def size_multiplier(self, returns_matrix: np.ndarray) -> float:
        """Size-down multiplier based on brake state.

        Returns 1.0 if brake clear, else 0.5 (halve exposure) when max
        correlation exceeds threshold. More sophisticated curves can land
        later — this is the conservative default.
        """
        verdict = self.correlation_brake(returns_matrix)
        return 0.5 if verdict["brake_engaged"] else 1.0
