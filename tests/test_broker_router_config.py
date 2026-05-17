from __future__ import annotations

from pathlib import Path

from eta_engine.scripts.broker_router_config import RoutingConfig, _asset_class_for_symbol, normalize_symbol


def test_asset_class_for_symbol_handles_futures_and_crypto_aliases() -> None:
    assert _asset_class_for_symbol("MNQ1") == "futures"
    assert _asset_class_for_symbol("MNQM6") == "futures"
    assert _asset_class_for_symbol("BTC/USD") == "crypto"
    assert _asset_class_for_symbol("ETHUSDT") == "crypto"
    assert _asset_class_for_symbol("SPY") == "equity"


def test_normalize_symbol_delegates_to_loaded_routing_config(monkeypatch) -> None:
    cfg = RoutingConfig(
        default_venue="ibkr",
        symbol_overrides={"BTC": {"ibkr": "BTCUSD"}},
        per_bot={},
    )
    monkeypatch.setattr(RoutingConfig, "load", classmethod(lambda cls, path=None: cfg))

    assert normalize_symbol("BTC", "ibkr") == "BTCUSD"
