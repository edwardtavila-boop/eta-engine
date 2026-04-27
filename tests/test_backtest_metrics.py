"""Tests for ``eta_engine.backtest.metrics``.

Auto-scaffolded by scripts/_test_scaffold.py -- the import smoke and
the per-symbol smoke tests are boilerplate. Edit freely; the
operator-specific edge cases belong here.
"""

from __future__ import annotations

import importlib


def test_import_smoke() -> None:
    """Module imports without raising."""
    importlib.import_module("eta_engine.backtest.metrics")


def test_compute_sharpe_smoke() -> None:
    """``compute_sharpe`` is callable (signature requires manual fill-in)."""
    from eta_engine.backtest.metrics import compute_sharpe

    assert callable(compute_sharpe)
    # TODO: invoke with realistic inputs and assert on output


def test_compute_sortino_smoke() -> None:
    """``compute_sortino`` is callable (signature requires manual fill-in)."""
    from eta_engine.backtest.metrics import compute_sortino

    assert callable(compute_sortino)
    # TODO: invoke with realistic inputs and assert on output


def test_compute_profit_factor_smoke() -> None:
    """``compute_profit_factor`` is callable (signature requires manual fill-in)."""
    from eta_engine.backtest.metrics import compute_profit_factor

    assert callable(compute_profit_factor)
    # TODO: invoke with realistic inputs and assert on output


def test_compute_max_dd_smoke() -> None:
    """``compute_max_dd`` is callable (signature requires manual fill-in)."""
    from eta_engine.backtest.metrics import compute_max_dd

    assert callable(compute_max_dd)
    # TODO: invoke with realistic inputs and assert on output


def test_compute_expectancy_smoke() -> None:
    """``compute_expectancy`` is callable (signature requires manual fill-in)."""
    from eta_engine.backtest.metrics import compute_expectancy

    assert callable(compute_expectancy)
    # TODO: invoke with realistic inputs and assert on output
