"""Drift tests between the routing yaml and the IBKR contract map.

These tests catch the failure mode where someone adds a symbol to
``configs/bot_broker_routing.yaml`` (e.g. enabling a new commodity) but
forgets to add the corresponding ``FUTURES_MAP`` / ``CRYPTO_MAP`` entry
in :mod:`eta_engine.venues.ibkr_live`. Without these tests, the
inconsistency would only surface at order time — and only for the
unlucky bot that hits the new symbol first.

The smoke harness (``ibkr_paper_smoke_all_assets``) provides the live
counterpart: this is the offline drift check, that's the live probe.
"""

from __future__ import annotations

from eta_engine.scripts.broker_router import RoutingConfig
from eta_engine.scripts.ibkr_paper_smoke_all_assets import DEFAULT_SMOKE_SYMBOLS
from eta_engine.venues.ibkr_live import CRYPTO_MAP, FUTURES_MAP


def test_default_smoke_symbols_all_have_contract_map_entries() -> None:
    """Every symbol in the default smoke set must resolve to a contract."""
    missing: list[str] = []
    for sym in DEFAULT_SMOKE_SYMBOLS:
        if sym not in FUTURES_MAP and sym not in CRYPTO_MAP:
            missing.append(sym)

    assert not missing, (
        f"default smoke symbols missing FUTURES_MAP/CRYPTO_MAP entries: {missing}. "
        f"Add them to eta_engine/venues/ibkr_live.py before smoke can run."
    )


def test_routing_yaml_ibkr_targets_all_have_contract_map_entries() -> None:
    """Every IBKR-routed symbol in the routing yaml must have a contract.

    This is the inverse drift check: if a bot is routed to ibkr in
    ``bot_broker_routing.yaml``, the symbol must resolve via
    FUTURES_MAP or CRYPTO_MAP. Otherwise place_order would crash on
    that bot's first signal.
    """
    cfg = RoutingConfig.load()
    missing: list[tuple[str, str]] = []  # (raw_symbol, why)

    for raw_symbol, venue_overrides in cfg.symbol_overrides.items():
        ibkr_target = venue_overrides.get("ibkr")
        if ibkr_target is None:
            continue
        target = ibkr_target.upper().strip().lstrip("/")
        if target not in FUTURES_MAP and target not in CRYPTO_MAP:
            missing.append((raw_symbol, f"ibkr target {target!r} unmapped"))

    assert not missing, (
        f"routing yaml has IBKR targets without contract map entries: {missing}. "
        f"Either add the contract to ibkr_live.py or remove the override."
    )


def test_critical_futures_universe_is_routable() -> None:
    """The 11 contracts the active fleet trades must all be IBKR-routable."""
    fleet_universe = (
        "MNQ", "NQ", "ES", "RTY", "M2K",   # equity index (incl. micros)
        "GC", "MGC",                        # gold
        "CL", "MCL",                        # crude oil
        "NG",                               # nat gas
        "6E", "M6E",                        # FX (Euro)
    )
    missing = [s for s in fleet_universe if s not in FUTURES_MAP]

    assert not missing, (
        f"core fleet futures missing from FUTURES_MAP: {missing}. "
        f"These are the live commodities/index/FX assets the bots can trade."
    )
