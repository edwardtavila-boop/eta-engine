from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eta_engine.venues import connection as connection_mod
from eta_engine.venues.base import ConnectionStatus, VenueConnectionReport
from eta_engine.venues.connection import (
    BrokerConnectionManager,
    BrokerConnectionSummary,
    canonical_broker_connections_hash_payload,
)

if TYPE_CHECKING:
    import pytest


def test_configured_brokers_excludes_dormant_futures_brokers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_load_config(_path: Path) -> dict[str, Any]:
        return {
            "execution": {
                "futures": {
                    "broker_primary": "IBKR",
                    "broker_backup": "TASTYTRADE",
                    "broker_backups": ["tradovate", "ibkr"],
                    "broker_dormant": ["tradovate"],
                },
            },
        }

    monkeypatch.setattr(connection_mod, "_load_config", fake_load_config)

    manager = BrokerConnectionManager(
        config_path=Path("C:/EvolutionaryTradingAlgo/eta_engine/config.json"),
        bybit_testnet=True,
        tradovate_demo=True,
    )

    assert manager.configured_brokers() == ["ibkr", "tastytrade"]


def test_connect_name_blocks_dormant_tradovate_before_adapter_probe() -> None:
    manager = BrokerConnectionManager(
        config_path=Path("C:/EvolutionaryTradingAlgo/eta_engine/config.json"),
        bybit_testnet=True,
        tradovate_demo=True,
    )

    report = asyncio.run(manager.connect_name("tradovate"))

    assert report.status is ConnectionStatus.FAILED
    assert report.details["policy_state"] == "dormant"
    assert report.details["active_substitute"] == "ibkr"
    assert "DORMANT" in report.error


def test_connect_name_blocks_us_person_non_fcm_venues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connection_mod, "IS_US_PERSON", True)
    manager = BrokerConnectionManager(
        config_path=Path("C:/EvolutionaryTradingAlgo/eta_engine/config.json"),
        bybit_testnet=True,
        tradovate_demo=True,
    )

    report = asyncio.run(manager.connect_name("bybit"))

    assert report.status is ConnectionStatus.FAILED
    assert report.details["policy_state"] == "blocked_us_person"
    assert report.details["active_substitute"] == "ibkr"
    assert "US-person live readiness" in report.error


def test_connect_name_reports_unimplemented_broker_as_unavailable() -> None:
    manager = BrokerConnectionManager(
        config_path=Path("C:/EvolutionaryTradingAlgo/eta_engine/config.json"),
        bybit_testnet=True,
        tradovate_demo=True,
    )

    report = asyncio.run(manager.connect_name("unknown_broker"))

    assert report.status is ConnectionStatus.UNAVAILABLE
    assert report.creds_present is False
    assert report.details["reason"] == "adapter not implemented in the current repo"


def test_connection_summary_counts_health_and_hash_payload_contract() -> None:
    summary = BrokerConnectionSummary(
        generated_at_utc=datetime(2026, 4, 29, tzinfo=UTC),
        configured_brokers=["ibkr", "tastytrade", "unknown"],
        config_path="C:/EvolutionaryTradingAlgo/eta_engine/config.json",
        reports=[
            VenueConnectionReport("ibkr", ConnectionStatus.READY, True),
            VenueConnectionReport("tastytrade", ConnectionStatus.DEGRADED, True),
            VenueConnectionReport("unknown", ConnectionStatus.UNAVAILABLE, False),
        ],
    )

    payload = summary.to_dict()

    assert summary.counts() == {
        "ready": 1,
        "degraded": 1,
        "stubbed": 0,
        "failed": 0,
        "unavailable": 1,
    }
    assert summary.overall_ok() is True
    assert summary.health() == "YELLOW"
    assert payload["summary"]["overall_ok"] is True
    assert payload["policy"]["active_futures_brokers"] == ["ibkr", "tastytrade"]

    payload["broker_connections_sha256"] = "ignored-for-stable-hash"
    assert "broker_connections_sha256" not in canonical_broker_connections_hash_payload(payload)
