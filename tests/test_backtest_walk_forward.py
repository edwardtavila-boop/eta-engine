"""Tests for ``eta_engine.backtest.walk_forward``.

Auto-scaffolded by scripts/_test_scaffold.py -- the import smoke and
the per-symbol smoke tests are boilerplate. Edit freely; the
operator-specific edge cases belong here.
"""

from __future__ import annotations

import importlib

import pytest


def test_import_smoke() -> None:
    """Module imports without raising."""
    importlib.import_module("eta_engine.backtest.walk_forward")


def test_walk_forward_config_smoke() -> None:
    """``WalkForwardConfig`` instantiates with no args (or skips if it requires args)."""
    from eta_engine.backtest.walk_forward import WalkForwardConfig

    try:
        obj = WalkForwardConfig()  # type: ignore[call-arg]
    except Exception as e:  # noqa: BLE001 -- pydantic/dataclass/attrs all raise differently
        pytest.skip(f"WalkForwardConfig requires args: {type(e).__name__}: {e}")
    else:
        assert obj is not None
        # TODO: real assertions about default state


def test_walk_forward_result_smoke() -> None:
    """``WalkForwardResult`` instantiates with no args (or skips if it requires args)."""
    from eta_engine.backtest.walk_forward import WalkForwardResult

    try:
        obj = WalkForwardResult()  # type: ignore[call-arg]
    except Exception as e:  # noqa: BLE001 -- pydantic/dataclass/attrs all raise differently
        pytest.skip(f"WalkForwardResult requires args: {type(e).__name__}: {e}")
    else:
        assert obj is not None
        # TODO: real assertions about default state


def test_walk_forward_engine_smoke() -> None:
    """``WalkForwardEngine`` instantiates with no args (or skips if it requires args)."""
    from eta_engine.backtest.walk_forward import WalkForwardEngine

    try:
        obj = WalkForwardEngine()  # type: ignore[call-arg]
    except Exception as e:  # noqa: BLE001 -- pydantic/dataclass/attrs all raise differently
        pytest.skip(f"WalkForwardEngine requires args: {type(e).__name__}: {e}")
    else:
        assert obj is not None
        # TODO: real assertions about default state
