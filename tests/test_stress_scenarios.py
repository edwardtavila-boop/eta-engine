"""Stress scenario generator tests — P3_PROOF stress."""

from __future__ import annotations

import numpy as np
import pytest

from eta_engine.backtest.stress_scenarios import (
    generate,
    generate_all,
    scenario_2008_slow_grind,
    scenario_2020_flash_crash,
    scenario_2022_regime_change,
)

# ---------------------------------------------------------------------------
# Individual scenarios
# ---------------------------------------------------------------------------


def test_2008_slow_grind_produces_negative_drift() -> None:
    returns, spec = scenario_2008_slow_grind(n_bars=250, seed=42)
    assert returns.shape == (250,)
    assert spec.kind == "2008_slow_grind"
    # Net return should be negative (relief rallies dampen the grind, so the
    # threshold is loose — the signature we care about is deep drawdown, not
    # final equity).
    assert spec.total_return < 0.0
    # Drawdown should be deep (>5%) — this is the actual scenario signature.
    assert spec.max_drawdown > 0.05


def test_2020_flash_crash_has_visible_shock() -> None:
    returns, spec = scenario_2020_flash_crash(n_bars=120, seed=42, crash_magnitude=-0.10)
    assert returns.shape == (120,)
    assert spec.kind == "2020_flash_crash"
    # Crash bar should carry the explicit shock
    assert returns.min() <= -0.10 + 1e-9
    # Max drawdown should be close to crash magnitude
    assert spec.max_drawdown >= 0.09


def test_2020_flash_crash_recovers_partially() -> None:
    returns, _ = scenario_2020_flash_crash(n_bars=120, seed=7)
    equity = np.cumprod(1.0 + returns)
    trough = equity.min()
    final = equity[-1]
    # Final equity should be above the trough (recovery path present)
    assert final > trough * 1.05


def test_2022_regime_change_has_rising_vol() -> None:
    returns, spec = scenario_2022_regime_change(n_bars=300, seed=99)
    assert returns.shape == (300,)
    assert spec.kind == "2022_regime_change"
    # Second half of series should have higher absolute volatility than first
    first_vol = float(np.std(returns[:150]))
    second_vol = float(np.std(returns[150:]))
    assert second_vol > first_vol


# ---------------------------------------------------------------------------
# Dispatcher + reproducibility
# ---------------------------------------------------------------------------


def test_generate_dispatches_by_name() -> None:
    r1, _ = generate("2008_slow_grind", n_bars=100)
    assert r1.shape == (100,)
    r2, _ = generate("2020_flash_crash", n_bars=80)
    assert r2.shape == (80,)
    r3, _ = generate("2022_regime_change", n_bars=60)
    assert r3.shape == (60,)


def test_generate_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown scenario"):
        generate("1987_black_monday")  # type: ignore[arg-type]


def test_seeded_generation_is_reproducible() -> None:
    a, _ = scenario_2022_regime_change(n_bars=50, seed=777)
    b, _ = scenario_2022_regime_change(n_bars=50, seed=777)
    np.testing.assert_array_equal(a, b)


def test_generate_all_returns_three_scenarios() -> None:
    bundle = generate_all(n_bars_each=100)
    assert set(bundle.keys()) == {
        "2008_slow_grind",
        "2020_flash_crash",
        "2022_regime_change",
    }
    for returns, spec in bundle.values():
        assert returns.shape == (100,)
        assert spec.n_bars == 100
