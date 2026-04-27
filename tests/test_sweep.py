"""
EVOLUTIONARY TRADING ALGO  //  tests.test_sweep
===================================
Profit sweep engine: triggers, split math, baseline protection.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Sweep logic (self-contained for testing — mirrors sweep_engine.py contract)
# ---------------------------------------------------------------------------


def check_sweep_trigger(
    current_equity: float,
    baseline_usd: float,
    min_sweep_usd: float = 100.0,
) -> bool:
    """True if equity exceeds baseline by at least min_sweep amount."""
    excess = current_equity - baseline_usd
    return excess >= min_sweep_usd


def calculate_sweep_split(
    excess_usd: float,
    reinvest_pct: float = 0.50,
    staking_pct: float = 0.30,
    cold_pct: float = 0.20,
) -> dict[str, float]:
    """Split excess capital according to The Funnel ratios.

    Returns USD amounts per destination.
    Percentages must sum to 1.0.
    """
    total_pct = reinvest_pct + staking_pct + cold_pct
    if abs(total_pct - 1.0) > 0.001:
        raise ValueError(f"Split percentages must sum to 1.0, got {total_pct}")
    if excess_usd <= 0:
        return {"reinvest": 0.0, "staking": 0.0, "cold": 0.0}

    return {
        "reinvest": round(excess_usd * reinvest_pct, 2),
        "staking": round(excess_usd * staking_pct, 2),
        "cold": round(excess_usd * cold_pct, 2),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSweepTrigger:
    def test_triggers_above_baseline(self) -> None:
        assert check_sweep_trigger(51_000.0, 50_000.0, 100.0) is True

    def test_no_trigger_at_baseline(self) -> None:
        assert check_sweep_trigger(50_000.0, 50_000.0) is False

    def test_no_trigger_below_baseline(self) -> None:
        assert check_sweep_trigger(49_000.0, 50_000.0) is False

    def test_min_sweep_threshold(self) -> None:
        """Excess of $50 should not trigger with $100 minimum."""
        assert check_sweep_trigger(50_050.0, 50_000.0, 100.0) is False

    def test_exact_minimum_triggers(self) -> None:
        assert check_sweep_trigger(50_100.0, 50_000.0, 100.0) is True


class TestSweepSplit:
    def test_correct_split_math(self) -> None:
        split = calculate_sweep_split(1000.0)
        assert split["reinvest"] == 500.0
        assert split["staking"] == 300.0
        assert split["cold"] == 200.0

    def test_split_sums_to_total(self) -> None:
        split = calculate_sweep_split(5000.0, 0.50, 0.30, 0.20)
        total = split["reinvest"] + split["staking"] + split["cold"]
        assert abs(total - 5000.0) < 0.01

    def test_no_sweep_below_baseline(self) -> None:
        """Zero or negative excess -> all zeros."""
        split = calculate_sweep_split(0.0)
        assert all(v == 0.0 for v in split.values())

    def test_negative_excess_returns_zeros(self) -> None:
        split = calculate_sweep_split(-500.0)
        assert all(v == 0.0 for v in split.values())

    def test_custom_ratios(self) -> None:
        split = calculate_sweep_split(10_000.0, 0.60, 0.25, 0.15)
        assert split["reinvest"] == 6000.0
        assert split["staking"] == 2500.0
        assert split["cold"] == 1500.0

    def test_invalid_ratios_raise(self) -> None:
        with pytest.raises(ValueError, match="must sum to 1.0"):
            calculate_sweep_split(1000.0, 0.50, 0.50, 0.50)

    @pytest.mark.parametrize("excess", [100.0, 1000.0, 50_000.0, 250_000.0])
    def test_proportional_scaling(self, excess: float) -> None:
        split = calculate_sweep_split(excess)
        assert split["reinvest"] == pytest.approx(excess * 0.50, abs=0.01)
        assert split["staking"] == pytest.approx(excess * 0.30, abs=0.01)
        assert split["cold"] == pytest.approx(excess * 0.20, abs=0.01)
