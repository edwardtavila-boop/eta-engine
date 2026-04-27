"""Tests for backtest.synthetic_bridge."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eta_engine.backtest.synthetic_bridge import (
    SCENARIO_REGIME_MAP,
    bars_from_returns,
    scenario_to_regime,
    synthetic_scenario_bars,
)
from eta_engine.brain.regime import RegimeType


class TestScenarioRegimeMap:
    def test_all_scenarios_have_mapping(self):
        for kind in ("2008_slow_grind", "2020_flash_crash", "2022_regime_change"):
            assert kind in SCENARIO_REGIME_MAP

    def test_flash_crash_is_crisis(self):
        assert scenario_to_regime("2020_flash_crash") == RegimeType.CRISIS

    def test_slow_grind_is_high_vol(self):
        assert scenario_to_regime("2008_slow_grind") == RegimeType.HIGH_VOL


class TestBarsFromReturns:
    def test_empty_returns_empty_bars(self):
        bars = bars_from_returns([], start_price=100.0, regime=RegimeType.HIGH_VOL, seed=42)
        assert bars == []

    def test_returns_materialize_into_n_bars(self):
        returns = [0.01, -0.02, 0.005, -0.015]
        bars = bars_from_returns(returns, start_price=100.0, regime=RegimeType.HIGH_VOL, seed=42)
        assert len(bars) == 4

    def test_open_chains_to_previous_close(self):
        returns = [0.01, -0.02, 0.005]
        bars = bars_from_returns(returns, start_price=100.0, regime=RegimeType.HIGH_VOL, seed=42)
        for i in range(1, len(bars)):
            assert bars[i].open == pytest.approx(bars[i - 1].close)

    def test_ohlc_invariants(self):
        returns = [0.05, -0.10, 0.02]  # volatile
        bars = bars_from_returns(returns, start_price=1000.0, regime=RegimeType.CRISIS, seed=7)
        for b in bars:
            assert b.high >= max(b.open, b.close)
            assert b.low <= min(b.open, b.close)
            assert b.low > 0

    def test_determinism_same_seed_same_bars(self):
        returns = [0.01, -0.02, 0.005]
        b1 = bars_from_returns(returns, start_price=100.0, regime=RegimeType.HIGH_VOL, seed=42)
        b2 = bars_from_returns(returns, start_price=100.0, regime=RegimeType.HIGH_VOL, seed=42)
        for a, b in zip(b1, b2, strict=True):
            assert a.high == b.high
            assert a.low == b.low
            assert a.volume == b.volume

    def test_rejects_zero_start_price(self):
        with pytest.raises(ValueError):
            bars_from_returns([0.01], start_price=0.0, regime=RegimeType.HIGH_VOL, seed=0)


class TestSyntheticScenarioBars:
    def test_flash_crash_produces_bars(self):
        bars, spec = synthetic_scenario_bars(
            "2020_flash_crash",
            n_bars=120,
            start_price=4500.0,
            seed=2020,
        )
        assert len(bars) == 120
        assert spec.kind == "2020_flash_crash"

    def test_bars_reflect_crash_magnitude(self):
        bars, spec = synthetic_scenario_bars(
            "2020_flash_crash",
            n_bars=120,
            start_price=4500.0,
            seed=2020,
        )
        # Find the worst bar-over-bar drop
        worst_drop = min((b.close / bars[i - 1].close - 1.0) for i, b in enumerate(bars) if i > 0)
        # Flash-crash scenario imposes at least a -10% bar
        assert worst_drop < -0.05

    def test_slow_grind_has_negative_total_return(self):
        _, spec = synthetic_scenario_bars("2008_slow_grind", n_bars=250, start_price=100.0, seed=2008)
        assert spec.total_return < 0

    def test_custom_start_ts(self):
        start = datetime(2024, 3, 11, tzinfo=UTC)
        bars, _ = synthetic_scenario_bars(
            "2022_regime_change",
            n_bars=50,
            start_price=100.0,
            seed=2022,
            start_ts=start,
        )
        assert bars[0].ts == start
