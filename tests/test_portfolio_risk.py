"""Portfolio VaR / CVaR + correlation-brake tests — P4_SHIELD.

Covers :class:`eta_engine.core.portfolio_risk.PortfolioRisk` across:
* single-series VaR (historical + parametric) and CVaR
* portfolio VaR with explicit + default (equal) weights
* correlation brake edge cases (degenerate shape, low / high correlation)
* size_multiplier halving when the brake trips
* guardrails (bad confidence, bad weights, too-few observations)
"""

from __future__ import annotations

import numpy as np
import pytest

from eta_engine.core.portfolio_risk import PortfolioRisk


@pytest.fixture()
def pr() -> PortfolioRisk:
    # min_observations kept small for deterministic fixtures.
    return PortfolioRisk(confidence_level=0.95, brake_correlation_threshold=0.70, min_observations=20)


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def test_bad_confidence_level_rejected() -> None:
    with pytest.raises(ValueError, match="confidence_level"):
        PortfolioRisk(confidence_level=0.3)
    with pytest.raises(ValueError, match="confidence_level"):
        PortfolioRisk(confidence_level=1.0)


def test_var_returns_zero_when_sample_too_small(pr: PortfolioRisk) -> None:
    returns = np.array([0.01, -0.02, 0.005])
    assert pr.var_historical(returns) == 0.0
    assert pr.var_parametric(returns) == 0.0
    assert pr.cvar(returns) == 0.0


# ---------------------------------------------------------------------------
# Single-series VaR / CVaR
# ---------------------------------------------------------------------------


def test_var_historical_is_loss_as_positive(pr: PortfolioRisk) -> None:
    # 100 returns: 10 losses at -0.05, rest flat. With 100 samples, the 5th
    # percentile is interpolated at index 4.95 between values[4] and values[5] —
    # both sit in the -0.05 block, so the interpolation lands exactly at -0.05.
    returns = np.concatenate([np.full(10, -0.05), np.zeros(90)])
    var = pr.var_historical(returns)
    # 5th percentile = -0.05  →  VaR = +0.05
    assert var == pytest.approx(0.05, abs=1e-9)


def test_var_parametric_matches_normal_formula(pr: PortfolioRisk) -> None:
    rng = np.random.default_rng(42)
    returns = rng.normal(0.0, 0.01, size=500)
    var = pr.var_parametric(returns)
    # Gaussian VaR should be close to 1.645 * sigma for 95%
    assert var == pytest.approx(1.645 * 0.01, rel=0.15)


def test_cvar_deeper_than_var(pr: PortfolioRisk) -> None:
    rng = np.random.default_rng(7)
    returns = rng.normal(0.0, 0.02, size=1000)
    var = pr.var_historical(returns)
    cvar = pr.cvar(returns)
    assert cvar >= var  # expected shortfall can equal VaR only in pathological cases
    assert cvar > 0


# ---------------------------------------------------------------------------
# Portfolio VaR
# ---------------------------------------------------------------------------


def test_portfolio_var_equal_weight_default(pr: PortfolioRisk) -> None:
    rng = np.random.default_rng(0)
    mat = rng.normal(0.0, 0.01, size=(500, 3))
    var = pr.portfolio_var(mat)
    assert var > 0


def test_portfolio_var_rejects_non_2d(pr: PortfolioRisk) -> None:
    with pytest.raises(ValueError, match="2D"):
        pr.portfolio_var(np.zeros(50))


def test_portfolio_var_rejects_bad_weights(pr: PortfolioRisk) -> None:
    mat = np.zeros((100, 3))
    with pytest.raises(ValueError, match="weights length"):
        pr.portfolio_var(mat, weights=np.array([0.5, 0.5]))
    with pytest.raises(ValueError, match="sum to 1"):
        pr.portfolio_var(mat, weights=np.array([0.5, 0.5, 0.5]))


def test_portfolio_var_weighted_concentration_raises_var(pr: PortfolioRisk) -> None:
    # Bot 0 has high variance, bot 1 is flat.
    rng = np.random.default_rng(1)
    vol = rng.normal(0.0, 0.05, size=500)
    flat = np.zeros(500)
    mat = np.column_stack([vol, flat])
    equal_var = pr.portfolio_var(mat, weights=np.array([0.5, 0.5]))
    concentrated_var = pr.portfolio_var(mat, weights=np.array([1.0, 0.0]))
    assert concentrated_var > equal_var


# ---------------------------------------------------------------------------
# Correlation brake
# ---------------------------------------------------------------------------


def test_correlation_brake_degenerate_shape(pr: PortfolioRisk) -> None:
    verdict = pr.correlation_brake(np.zeros((100, 1)))
    assert verdict["brake_engaged"] is False
    assert verdict["max_correlation"] == 0.0


def test_correlation_brake_low_correlation_clear(pr: PortfolioRisk) -> None:
    rng = np.random.default_rng(5)
    mat = rng.normal(0.0, 0.01, size=(300, 4))  # independent streams
    verdict = pr.correlation_brake(mat)
    assert verdict["brake_engaged"] is False
    assert float(verdict["max_correlation"]) < 0.70


def test_correlation_brake_high_correlation_engages(pr: PortfolioRisk) -> None:
    rng = np.random.default_rng(9)
    base = rng.normal(0.0, 0.01, size=300)
    # Two near-identical bots (noise ~1% of signal).
    mat = np.column_stack([base, base + rng.normal(0.0, 1e-5, size=300)])
    verdict = pr.correlation_brake(mat)
    assert verdict["brake_engaged"] is True
    assert float(verdict["max_correlation"]) > 0.95


def test_size_multiplier_halves_on_brake(pr: PortfolioRisk) -> None:
    rng = np.random.default_rng(11)
    base = rng.normal(0.0, 0.01, size=300)
    mat = np.column_stack([base, base + rng.normal(0.0, 1e-5, size=300)])
    assert pr.size_multiplier(mat) == 0.5


def test_size_multiplier_full_when_clear(pr: PortfolioRisk) -> None:
    rng = np.random.default_rng(13)
    mat = rng.normal(0.0, 0.01, size=(300, 3))
    assert pr.size_multiplier(mat) == 1.0
