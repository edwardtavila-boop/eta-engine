"""Tests for ``apex_predator.backtest.engine``.

Auto-scaffolded by scripts/_test_scaffold.py -- the import smoke and
the per-symbol smoke tests are boilerplate. Edit freely; the
operator-specific edge cases belong here.
"""
from __future__ import annotations

import importlib


def test_import_smoke() -> None:
    """Module imports without raising."""
    importlib.import_module("apex_predator.backtest.engine")


def test_backtest_engine_smoke() -> None:
    """``BacktestEngine`` instantiates with a minimal valid pipeline + config."""
    from datetime import UTC, datetime

    from apex_predator.backtest.engine import BacktestEngine
    from apex_predator.backtest.models import BacktestConfig
    from apex_predator.features.pipeline import FeaturePipeline

    pipeline = FeaturePipeline()
    config = BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 1, 31, tzinfo=UTC),
        symbol="MNQ",
        initial_equity=5_000.0,
        risk_per_trade_pct=0.01,
    )
    engine = BacktestEngine(pipeline=pipeline, config=config)
    assert engine.pipeline is pipeline
    assert engine.config is config
    assert engine.strategy_id == "apex_default"
    # ctx_builder defaults to a no-op lambda when not supplied.
    assert callable(engine.ctx_builder)
    assert engine.ctx_builder(None, []) == {}
