"""
EVOLUTIONARY TRADING ALGO  //  tests.test_grid
==================================
Geometric grid calculation tests.
Test the math — geometric spacing, level counts, capital distribution.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Grid math (self-contained — no external module dependency)
# ---------------------------------------------------------------------------


def geometric_grid(
    lower: float,
    upper: float,
    num_levels: int,
    total_capital: float,
) -> list[dict[str, float]]:
    """Calculate geometric grid levels between lower and upper bounds.

    Geometric spacing: each level is ratio * previous level.
    ratio = (upper / lower) ^ (1 / (num_levels - 1))
    """
    if num_levels < 2:
        raise ValueError("Need at least 2 grid levels")
    if lower >= upper:
        raise ValueError("Lower bound must be below upper bound")
    if lower <= 0:
        raise ValueError("Lower bound must be positive")

    ratio = (upper / lower) ** (1.0 / (num_levels - 1))
    capital_per_level = total_capital / num_levels

    levels = []
    for i in range(num_levels):
        price = lower * (ratio**i)
        levels.append(
            {
                "level": i,
                "price": round(price, 2),
                "capital_usd": round(capital_per_level, 2),
                "ratio_from_prev": round(ratio, 6) if i > 0 else None,
            }
        )
    return levels


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGeometricGrid:
    def test_correct_number_of_levels(self) -> None:
        grid = geometric_grid(20000.0, 22000.0, 10, 5000.0)
        assert len(grid) == 10

    def test_first_level_at_lower(self) -> None:
        grid = geometric_grid(20000.0, 22000.0, 5, 1000.0)
        assert grid[0]["price"] == 20000.0

    def test_last_level_at_upper(self) -> None:
        grid = geometric_grid(20000.0, 22000.0, 5, 1000.0)
        assert abs(grid[-1]["price"] - 22000.0) < 1.0

    def test_geometric_spacing_ratio(self) -> None:
        """Each consecutive pair should have the same ratio."""
        grid = geometric_grid(100.0, 200.0, 8, 8000.0)
        ratios = []
        for i in range(1, len(grid)):
            ratios.append(grid[i]["price"] / grid[i - 1]["price"])
        # All ratios should be approximately equal
        for r in ratios:
            assert abs(r - ratios[0]) < 0.0001

    def test_capital_per_level(self) -> None:
        """Capital should be evenly distributed."""
        grid = geometric_grid(20000.0, 22000.0, 10, 5000.0)
        for level in grid:
            assert level["capital_usd"] == 500.0

    def test_total_capital_preserved(self) -> None:
        grid = geometric_grid(20000.0, 22000.0, 10, 5000.0)
        total = sum(level["capital_usd"] for level in grid)
        assert abs(total - 5000.0) < 0.01

    @pytest.mark.parametrize("levels", [2, 5, 10, 20, 50])
    def test_monotonic_increasing(self, levels: int) -> None:
        grid = geometric_grid(100.0, 1000.0, levels, levels * 100.0)
        prices = [level["price"] for level in grid]
        for i in range(1, len(prices)):
            assert prices[i] > prices[i - 1]

    def test_single_level_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            geometric_grid(100.0, 200.0, 1, 100.0)

    def test_inverted_bounds_raises(self) -> None:
        with pytest.raises(ValueError, match="below upper"):
            geometric_grid(200.0, 100.0, 5, 1000.0)

    def test_ratio_matches_expected(self) -> None:
        """Verify ratio formula: (upper/lower)^(1/(n-1))."""
        lower, upper, n = 100.0, 400.0, 5
        expected_ratio = (upper / lower) ** (1.0 / (n - 1))
        grid = geometric_grid(lower, upper, n, 5000.0)
        actual_ratio = grid[1]["price"] / grid[0]["price"]
        assert abs(actual_ratio - expected_ratio) < 0.0001
