"""Tests for asset-specific strategy routing helpers."""
from __future__ import annotations

from eta_engine.feeds.asset_specific_config import (
    get_asset_class,
    get_edge_preset_for_symbol,
    get_intermarket_for_symbol,
    normalize_symbol,
)


def test_normalize_symbol_handles_live_futures_contracts() -> None:
    assert normalize_symbol("MNQM6") == "MNQ"
    assert normalize_symbol("NQZ6") == "NQ"
    assert normalize_symbol("MCLM6") == "MCL"
    assert normalize_symbol("/NGK6") == "NG"
    assert normalize_symbol("6EM6") == "6E"


def test_normalize_symbol_handles_continuous_and_crypto_pairs() -> None:
    assert normalize_symbol("MNQ1") == "MNQ"
    assert normalize_symbol("ETHUSD") == "ETH"
    assert normalize_symbol("BTCUSDT") == "BTC"
    assert normalize_symbol("SOL-PERP") == "SOL"
    assert normalize_symbol("XRPUSD") == "XRP"


def test_asset_class_uses_normalized_live_symbols() -> None:
    assert get_asset_class("MNQM6") == "equity"
    assert get_asset_class("MCLM6") == "commodity"
    assert get_asset_class("ETHUSD") == "crypto"
    assert get_asset_class("6EM6") == "fx"


def test_intermarket_pairs_and_edge_presets_match_live_symbols() -> None:
    assert get_intermarket_for_symbol("MNQM6")[:2] == ["ES", "NQ"]
    assert get_intermarket_for_symbol("ETHUSD")[:2] == ["BTC", "DXY"]
    assert get_edge_preset_for_symbol("ETHUSD")["is_crypto"] is True
    assert get_edge_preset_for_symbol("MCLM6")["enable_absorption_gate"] is True
