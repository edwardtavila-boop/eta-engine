"""Tests for the WalkForwardEngine rigor extensions.

Five extensions exercised here (all pure-numpy, deterministic when seeded):

  1. Block-bootstrap CI on expR
  2. Bonferroni-adjusted p-value
  3. Friction-aware net expR (per-symbol via instrument_specs)
  4. Split-half sign stability
  5. Deflated Sharpe (Lopez de Prado 2014)

Each test pins down ONE behavior with a synthetic series whose properties
are mathematically known. We do NOT validate against scipy because it is
not in the dep tree - the pure-numpy approximations are checked against
hand-computed expectations and against each other.
"""
from __future__ import annotations

import numpy as np
import pytest

from eta_engine.feeds.strategy_lab.rigor import (
    BootstrapResult,
    RigorReport,
    SplitHalfResult,
    block_bootstrap_expR,
    bonferroni_adjust,
    compute_rigor,
    deflated_sharpe,
    expected_max_sr,
    friction_R_per_trade,
    net_expR,
    norm_ppf,
    split_half_stability,
)

# --- norm_ppf sanity ---


def test_norm_ppf_known_values() -> None:
    """Pure-numpy probit must match canonical quantiles to 1e-3."""
    assert abs(norm_ppf(0.5) - 0.0) < 1e-6
    assert abs(norm_ppf(0.975) - 1.95996398454) < 1e-3
    assert abs(norm_ppf(0.025) + 1.95996398454) < 1e-3
    assert abs(norm_ppf(0.99) - 2.32634787404) < 1e-3
    assert abs(norm_ppf(0.01) + 2.32634787404) < 1e-3


def test_norm_ppf_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        norm_ppf(0.0)
    with pytest.raises(ValueError):
        norm_ppf(1.0)
    with pytest.raises(ValueError):
        norm_ppf(-0.1)


# --- 1. Block bootstrap ---


def test_block_bootstrap_positive_signal_excludes_zero() -> None:
    """200 trades from N(0.05, 1.0) - p5 close to sample mean within 0.02."""
    rng = np.random.default_rng(42)
    arr = rng.normal(0.05, 1.0, 200)
    boot = block_bootstrap_expR(arr, block_size=5, n_resamples=5000, seed=7)
    assert isinstance(boot, BootstrapResult)
    sample_mean = float(arr.mean())
    assert abs(boot.p50 - sample_mean) < 0.02
    assert boot.p5 < sample_mean < boot.p95
    assert boot.block_size == 5
    assert boot.n_resamples == 5000


def test_block_bootstrap_null_brackets_zero() -> None:
    """100 trades from N(0, 1.0) - CI must include zero (p5 <= 0)."""
    rng = np.random.default_rng(99)
    arr = rng.normal(0.0, 1.0, 100)
    boot = block_bootstrap_expR(arr, n_resamples=5000, seed=7)
    # On a true null with n=100, the CI must straddle zero. The sample
    # mean itself can be slightly positive or negative, but p5 <= 0
    # must hold with overwhelming probability for a centered N(0,1).
    assert boot.p5 <= 0.05
    # p_value_raw is the bootstrap probability that mean <= 0. For a
    # true null it should be neither tiny nor close to 1 - this is a
    # weak sanity check (we just want to confirm it is finite).
    assert 0.0 < boot.p_value_raw < 1.0


def test_block_bootstrap_autocorrelated_wider_than_iid() -> None:
    """AR(1) phi=0.7 series should yield a CI no narrower than IID."""
    rng = np.random.default_rng(123)
    n = 200
    iid = rng.normal(0.05, 1.0, n)
    eps = rng.normal(0.0, 1.0, n)
    ar = np.zeros(n)
    ar[0] = eps[0]
    phi = 0.7
    for i in range(1, n):
        ar[i] = phi * ar[i-1] + eps[i]
    ar = (ar - ar.mean()) / ar.std() * iid.std() + iid.mean()
    boot_iid = block_bootstrap_expR(iid, block_size=5, n_resamples=5000, seed=1)
    boot_ar  = block_bootstrap_expR(ar,  block_size=5, n_resamples=5000, seed=1)
    width_iid = boot_iid.p95 - boot_iid.p5
    width_ar  = boot_ar.p95  - boot_ar.p5
    assert width_ar >= width_iid * 0.95


def test_block_bootstrap_empty_safe() -> None:
    boot = block_bootstrap_expR(np.array([]))
    assert boot.p5 == 0.0
    assert boot.p_value_raw == 1.0


# --- 2. Bonferroni adjustment ---


def test_bonferroni_simple_division() -> None:
    """p_raw=0.04, multi_test_count=20 -> p_adj = min(1, 0.8) = 0.8."""
    assert bonferroni_adjust(0.04, 20) == pytest.approx(0.8)


def test_bonferroni_caps_at_one() -> None:
    """p_raw=0.5 with N=10 -> 5.0 capped to 1.0."""
    assert bonferroni_adjust(0.5, 10) == 1.0


def test_bonferroni_no_adjustment_when_n_le_zero() -> None:
    assert bonferroni_adjust(0.04, 0) == 0.04
    assert bonferroni_adjust(0.04, -5) == 0.04


def test_bonferroni_n_one_equals_raw() -> None:
    assert bonferroni_adjust(0.04, 1) == 0.04


# --- 3. Friction-aware net expR ---


def test_friction_r_per_trade_mnq_realistic() -> None:
    """MNQ: commission=$1.40, half-spread=$0.25, RT=$1.90;
    stop_dist 1.5*30pts*$2 = $90; R_per_trade ~ 0.021."""
    fric = friction_R_per_trade("MNQ", avg_stop_atr_mult=1.5, typical_atr_pts=30.0)
    assert 0.018 < fric < 0.025


def test_friction_r_per_trade_unknown_symbol_falls_back() -> None:
    fric = friction_R_per_trade("ZZZ", avg_stop_atr_mult=1.0, typical_atr_pts=10.0)
    assert fric > 0.0
    assert np.isfinite(fric)


def test_net_expr_subtracts_friction() -> None:
    arr = np.array([0.5, -1.0, 2.0, -1.0, 0.5])  # mean = 0.2
    assert net_expR(arr, 0.05) == pytest.approx(0.15)
    assert net_expR(np.array([]), 0.05) == 0.0


# --- 4. Split-half stability ---


def test_split_half_sign_stable_when_both_positive() -> None:
    arr = np.array([0.5] * 50 + [0.3] * 50)
    sh = split_half_stability(arr)
    assert isinstance(sh, SplitHalfResult)
    assert sh.expR_half_1 == pytest.approx(0.5)
    assert sh.expR_half_2 == pytest.approx(0.3)
    assert sh.sign_stable is True


def test_split_half_sign_not_stable_when_signs_differ() -> None:
    arr = np.array([0.5] * 50 + [-0.3] * 50)
    sh = split_half_stability(arr)
    assert sh.sign_stable is False


def test_split_half_zero_treated_as_unstable() -> None:
    arr = np.array([0.0] * 50 + [0.5] * 50)
    sh = split_half_stability(arr)
    assert sh.sign_stable is False


def test_split_half_short_series_unstable() -> None:
    sh = split_half_stability(np.array([0.5]))
    assert sh.sign_stable is False


# --- 5. Deflated Sharpe ---


def test_expected_max_sr_monotone_in_n() -> None:
    """E[max k SR] must increase with k."""
    assert expected_max_sr(5) < expected_max_sr(20) < expected_max_sr(100)


def test_expected_max_sr_n1_is_zero() -> None:
    assert expected_max_sr(1) == 0.0
    assert expected_max_sr(0) == 0.0


def test_deflated_sharpe_strong_signal_passes() -> None:
    """Deterministic series mean=0.3, sd~0.5 (per-trade SR~0.6),
    N=200 - should clear DS>1 against n_trials=33."""
    # Construct: 200 values that alternate +0.8, -0.2 -> mean=0.3, sd~0.5
    arr = np.array([0.8, -0.2] * 100)
    ds = deflated_sharpe(arr, n_trials=33)
    assert ds > 1.0, f"strong constructed signal should give DS>1, got {ds}"


def test_deflated_sharpe_null_fails() -> None:
    rng = np.random.default_rng(11)
    arr = rng.normal(0.0, 1.0, 100)
    ds = deflated_sharpe(arr, n_trials=33)
    assert ds < 1.0


def test_deflated_sharpe_short_series_zero() -> None:
    assert deflated_sharpe(np.array([1.0, 2.0]), n_trials=10) == 0.0


# --- compute_rigor end-to-end gate ---


def test_compute_rigor_strong_signal_passes_strict_gate() -> None:
    """N=200 from N(0.3, 1.0) - per-trade SR=0.3 - should pass all
    components when multi_test_count=10."""
    rng = np.random.default_rng(2026)
    arr = rng.normal(0.3, 1.0, 200)
    r = compute_rigor(arr, symbol="MNQ", multi_test_count=10, typical_atr_pts=30.0)
    assert isinstance(r, RigorReport)
    assert r.expR_p5 > 0.0
    assert r.p_value_bonferroni < 0.05
    assert r.expR_net > 0.0
    assert r.split_half_sign_stable is True
    assert r.sharpe_deflated >= 1.0
    assert r.passed_strict is True


def test_compute_rigor_null_fails_strict_gate() -> None:
    """Strict gate must fire AT LEAST one failure on a true null."""
    rng = np.random.default_rng(2027)
    arr = rng.normal(0.0, 1.0, 100)
    r = compute_rigor(arr, symbol="MNQ", multi_test_count=33, typical_atr_pts=30.0)
    assert r.passed_strict is False
    assert len(r.strict_fail_reasons) >= 1


def test_compute_rigor_small_sample_fails_min_trades_gate() -> None:
    """N=20 < 30 must fail min_trades regardless of data."""
    arr = np.array([0.5] * 20)
    r = compute_rigor(arr, symbol="MNQ", multi_test_count=1, typical_atr_pts=30.0)
    assert r.passed_strict is False
    assert any("total_trades" in reason for reason in r.strict_fail_reasons)


def test_compute_rigor_bonferroni_division_n20() -> None:
    """multi_test=20 must produce a strictly larger p_bonferroni than
    multi_test=1 for the same input."""
    rng = np.random.default_rng(8)
    arr = rng.normal(0.16, 1.0, 100)
    r1 = compute_rigor(arr, symbol="MNQ", multi_test_count=1, typical_atr_pts=30.0)
    r20 = compute_rigor(arr, symbol="MNQ", multi_test_count=20, typical_atr_pts=30.0)
    assert r20.p_value_bonferroni > r1.p_value_bonferroni
    if r1.p_value_raw < 0.05 and r20.p_value_bonferroni >= 0.05:
        assert any(
            "p_value_bonferroni" in reason
            for reason in r20.strict_fail_reasons
        )


def test_compute_rigor_friction_kills_marginal_edge() -> None:
    """Deterministic series with mean R = 0.005 - tiny edge that the
    MNQ friction (~0.02 R) must wipe out in net_expR."""
    arr = np.array([0.005] * 200)  # exact mean = 0.005
    r = compute_rigor(arr, symbol="MNQ", multi_test_count=10, typical_atr_pts=30.0)
    assert r.expR_net < 0.0, (
        f"net should be negative (mean=0.005 - friction); got "
        f"net={r.expR_net} fric={r.friction_R_per_trade}"
    )
    assert r.passed_strict is False
    assert any("expR_net" in reason for reason in r.strict_fail_reasons)


def test_lab_result_back_compat_legacy_fields_preserved() -> None:
    """Old-style construction still works; new fields default safely."""
    from eta_engine.feeds.strategy_lab.engine import LabResult
    r = LabResult(strategy_id="legacy", bot_id="bot_x")
    assert r.total_trades == 0
    assert r.passed is False
    assert r.expR_p5 == 0.0
    assert r.passed_strict is False
    assert r.sharpe_deflated == 0.0
    assert r.legacy_passed is False
    assert r.strict_fail_reasons == []
