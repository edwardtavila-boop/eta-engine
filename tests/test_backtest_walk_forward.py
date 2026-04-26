"""Tests for ``apex_predator.backtest.walk_forward``.

Auto-scaffolded by scripts/_test_scaffold.py -- the import smoke and
the per-symbol smoke tests are boilerplate. Edit freely; the
operator-specific edge cases belong here.
"""
from __future__ import annotations

import importlib


def test_import_smoke() -> None:
    """Module imports without raising."""
    importlib.import_module("apex_predator.backtest.walk_forward")


def test_walk_forward_config_smoke() -> None:
    """``WalkForwardConfig`` instantiates with the two required fields."""
    from apex_predator.backtest.walk_forward import WalkForwardConfig

    cfg = WalkForwardConfig(window_days=30, step_days=7)
    assert cfg.window_days == 30
    assert cfg.step_days == 7
    # Defaults exposed for downstream callers.
    assert cfg.anchored is False
    assert cfg.oos_fraction == 0.3
    assert cfg.min_trades_per_window == 20
    assert cfg.strict_fold_dsr_gate is False
    assert cfg.fold_dsr_min_pass_fraction == 0.5


def test_walk_forward_result_smoke() -> None:
    """``WalkForwardResult`` defaults to an empty-windows summary."""
    from apex_predator.backtest.walk_forward import WalkForwardResult

    res = WalkForwardResult()
    assert res.windows == []
    assert res.aggregate_is_sharpe == 0.0
    assert res.aggregate_oos_sharpe == 0.0
    assert res.deflated_sharpe == 0.0
    assert res.pass_gate is False
    assert res.per_fold_dsr == []


def test_walk_forward_engine_smoke() -> None:
    """``WalkForwardEngine`` is a stateless engine -- no args to construct."""
    from apex_predator.backtest.walk_forward import (
        WalkForwardEngine,
        WalkForwardResult,
    )

    engine = WalkForwardEngine()
    # An empty bar list is the canonical zero-input edge case.
    res = engine.run(
        bars=[],
        pipeline=None,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        base_backtest_config=None,  # type: ignore[arg-type]
    )
    assert isinstance(res, WalkForwardResult)
    assert res.windows == []
    assert res.pass_gate is False
