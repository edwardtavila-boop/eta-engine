"""Tests for ``eta_engine.staking.allocator``.

Auto-scaffolded by scripts/_test_scaffold.py -- the import smoke and
the per-symbol smoke tests are boilerplate. Edit freely; the
operator-specific edge cases belong here.
"""

from __future__ import annotations

import importlib

import pytest


def test_import_smoke() -> None:
    """Module imports without raising."""
    importlib.import_module("eta_engine.staking.allocator")


def test_allocation_config_smoke() -> None:
    """``AllocationConfig`` instantiates with no args (or skips if it requires args)."""
    from eta_engine.staking.allocator import AllocationConfig

    try:
        obj = AllocationConfig()  # type: ignore[call-arg]
    except TypeError as e:
        pytest.skip(f"AllocationConfig requires args: {e}")
    else:
        assert obj is not None
        # TODO: real assertions about default state


def test_allocate_smoke() -> None:
    """``allocate`` is callable (signature requires manual fill-in)."""
    from eta_engine.staking.allocator import allocate

    assert callable(allocate)
    # TODO: invoke with realistic inputs and assert on output


@pytest.mark.asyncio
async def test_rebalance_smoke() -> None:
    """``rebalance`` is an async callable (signature requires manual fill-in)."""
    import inspect

    from eta_engine.staking.allocator import rebalance

    assert inspect.iscoroutinefunction(rebalance)
    # TODO: await with realistic inputs and assert on output
