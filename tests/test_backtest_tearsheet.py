"""Tests for ``eta_engine.backtest.tearsheet``.

Auto-scaffolded by scripts/_test_scaffold.py -- the import smoke and
the per-symbol smoke tests are boilerplate. Edit freely; the
operator-specific edge cases belong here.
"""

from __future__ import annotations

import importlib

import pytest


def test_import_smoke() -> None:
    """Module imports without raising."""
    importlib.import_module("eta_engine.backtest.tearsheet")


def test_tearsheet_builder_smoke() -> None:
    """``TearsheetBuilder`` instantiates with no args (or skips if it requires args)."""
    from eta_engine.backtest.tearsheet import TearsheetBuilder

    try:
        obj = TearsheetBuilder()  # type: ignore[call-arg]
    except TypeError as e:
        pytest.skip(f"TearsheetBuilder requires args: {e}")
    else:
        assert obj is not None
        # TODO: real assertions about default state
