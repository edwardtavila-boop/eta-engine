from __future__ import annotations

from types import SimpleNamespace

import pytest

from eta_engine.scripts.flatten_ibkr_positions import (
    _action_for_position,
    _patch_contract_exchange,
)


def test_action_for_position_maps_long_and_short() -> None:
    assert _action_for_position(3) == "SELL"
    assert _action_for_position(-2) == "BUY"


def test_action_for_position_rejects_zero() -> None:
    with pytest.raises(ValueError):
        _action_for_position(0)


def test_patch_contract_exchange_fills_missing_futures_exchange() -> None:
    contract = SimpleNamespace(symbol="MNQ", secType="FUT", exchange="")

    _patch_contract_exchange(contract)

    assert contract.exchange == "CME"


def test_patch_contract_exchange_leaves_existing_exchange_intact() -> None:
    contract = SimpleNamespace(symbol="GC", secType="FUT", exchange="COMEX")

    _patch_contract_exchange(contract)

    assert contract.exchange == "COMEX"
