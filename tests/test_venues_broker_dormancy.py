"""EVOLUTIONARY TRADING ALGO // tests.test_venues_broker_dormancy.

Broker dormancy policy tests (operator mandate 2026-04-24).

Tradovate is funding-blocked until further notice. The live futures
routing defaults must be IBKR primary + Tastytrade fallback. Dormant
brokers, even if explicitly requested via ``preferred_futures_venue``,
must be substituted with the active default and a warning logged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.venues import router as router_mod
from eta_engine.venues.ibkr import IbkrClientPortalVenue
from eta_engine.venues.router import (
    ACTIVE_FUTURES_VENUES,
    DEFAULT_FUTURES_VENUE,
    DORMANT_BROKERS,
    SmartRouter,
)
from eta_engine.venues.tastytrade import TastytradeVenue

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"


# --- Module-level policy constants ------------------------------------------


class TestBrokerPolicyConstants:
    def test_default_futures_venue_is_ibkr(self) -> None:
        assert DEFAULT_FUTURES_VENUE == "ibkr"

    def test_dormant_brokers_contains_tradovate(self) -> None:
        assert "tradovate" in DORMANT_BROKERS

    def test_active_futures_venues_excludes_dormant(self) -> None:
        dormant = DORMANT_BROKERS & set(ACTIVE_FUTURES_VENUES)
        assert dormant == set(), f"ACTIVE_FUTURES_VENUES leaks dormant brokers: {dormant}"

    def test_active_futures_venues_contains_ibkr_and_tastytrade(self) -> None:
        assert "ibkr" in ACTIVE_FUTURES_VENUES
        assert "tastytrade" in ACTIVE_FUTURES_VENUES


# --- SmartRouter default behavior -------------------------------------------


class TestSmartRouterDefaults:
    def test_default_constructor_routes_futures_to_ibkr(self) -> None:
        router = SmartRouter()
        assert isinstance(router.choose_venue("MNQM5", 1), IbkrClientPortalVenue)
        assert isinstance(router.choose_venue("NQM6", 1), IbkrClientPortalVenue)

    def test_default_preferred_futures_venue_attr_is_ibkr(self) -> None:
        router = SmartRouter()
        assert router._preferred_futures_venue == "ibkr"


# --- Dormant-request substitution -------------------------------------------


class TestDormantBrokerSubstitution:
    def test_explicit_tradovate_is_substituted_with_ibkr(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger=router_mod.__name__)
        router = SmartRouter(preferred_futures_venue="tradovate")
        # Substituted transparently
        assert router._preferred_futures_venue == DEFAULT_FUTURES_VENUE
        assert isinstance(router.choose_venue("MNQM5", 1), IbkrClientPortalVenue)
        # Warning emitted
        messages = [r.getMessage() for r in caplog.records]
        assert any("tradovate" in m and "DORMANT" in m for m in messages), (
            f"expected DORMANT substitution warning, got: {messages}"
        )

    def test_explicit_ibkr_not_substituted(self) -> None:
        router = SmartRouter(preferred_futures_venue="ibkr")
        assert router._preferred_futures_venue == "ibkr"

    def test_explicit_tastytrade_not_substituted(self) -> None:
        router = SmartRouter(preferred_futures_venue="tastytrade")
        assert router._preferred_futures_venue == "tastytrade"
        assert isinstance(router.choose_venue("MNQM5", 1), TastytradeVenue)

    def test_uppercase_dormant_also_substituted(self) -> None:
        # Case-insensitive dormancy match
        router = SmartRouter(preferred_futures_venue="TRADOVATE")
        assert router._preferred_futures_venue == DEFAULT_FUTURES_VENUE


# --- Config.json canonical policy -------------------------------------------


class TestConfigJsonBrokerPolicy:
    def test_config_broker_primary_is_ibkr(self) -> None:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        futures = cfg["execution"]["futures"]
        assert futures["broker_primary"] == "ibkr", (
            f"broker_primary must be 'ibkr' while Tradovate is DORMANT, got {futures['broker_primary']!r}"
        )

    def test_config_broker_dormant_field_contains_tradovate(self) -> None:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        futures = cfg["execution"]["futures"]
        dormant = futures.get("broker_dormant", [])
        assert "tradovate" in dormant, (
            f"config.json execution.futures.broker_dormant must list tradovate, got {dormant!r}"
        )

    def test_config_broker_backups_do_not_include_tradovate(self) -> None:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        futures = cfg["execution"]["futures"]
        backups = futures.get("broker_backups", [])
        assert "tradovate" not in backups, f"broker_backups must NOT include dormant tradovate, got {backups!r}"
