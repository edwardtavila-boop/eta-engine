"""Tests for ``apex_predator.staking.base``.

Auto-scaffolded by scripts/_test_scaffold.py -- the import smoke and
the per-symbol smoke tests are boilerplate. Edit freely; the
operator-specific edge cases belong here.
"""
from __future__ import annotations

import importlib

import pytest


def test_import_smoke() -> None:
    """Module imports without raising."""
    importlib.import_module("apex_predator.staking.base")


def test_staking_adapter_is_abstract() -> None:
    """``StakingAdapter`` is an ABC -- direct construction must raise."""
    from apex_predator.staking.base import StakingAdapter

    with pytest.raises(TypeError, match="abstract"):
        StakingAdapter()  # type: ignore[abstract]


def test_minimal_subclass_satisfies_contract() -> None:
    """A subclass that implements all four abstract methods constructs."""
    from apex_predator.staking.base import StakingAdapter

    class _Stub(StakingAdapter):
        symbol = "ETH"
        token = "wstETH"
        target_apy = 3.8

        async def stake(self, amount: float, token: str | None = None) -> str:
            return "tx-stake"

        async def unstake(self, amount: float) -> str:
            return "tx-unstake"

        async def get_balance(self) -> float:
            return 0.0

        async def get_apy(self) -> float:
            return self.target_apy

    obj = _Stub()
    assert obj.symbol == "ETH"
    assert obj.token == "wstETH"
    assert obj.target_apy == 3.8
    assert "ETH->wstETH" in repr(obj)
