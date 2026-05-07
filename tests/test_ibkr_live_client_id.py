from __future__ import annotations

from eta_engine.venues.ibkr_live import LiveIbkrVenue, ibkr_connect_timeout_seconds


def test_live_ibkr_venue_uses_default_client_id(monkeypatch) -> None:
    monkeypatch.delenv("ETA_IBKR_CLIENT_ID", raising=False)

    venue = LiveIbkrVenue()

    assert venue._client_id == 99


def test_live_ibkr_venue_reads_env_client_id(monkeypatch) -> None:
    monkeypatch.setenv("ETA_IBKR_CLIENT_ID", "104")

    venue = LiveIbkrVenue()

    assert venue._client_id == 104


def test_live_ibkr_venue_invalid_env_client_id_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("ETA_IBKR_CLIENT_ID", "not-an-int")

    venue = LiveIbkrVenue()

    assert venue._client_id == 99


def test_live_ibkr_venue_zero_env_client_id_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("ETA_IBKR_CLIENT_ID", "0")

    venue = LiveIbkrVenue()

    assert venue._client_id == 99


def test_live_ibkr_venue_negative_env_client_id_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("ETA_IBKR_CLIENT_ID", "-7")

    venue = LiveIbkrVenue()

    assert venue._client_id == 99


def test_live_ibkr_connect_timeout_defaults_to_20(monkeypatch) -> None:
    monkeypatch.delenv("ETA_IBKR_CONNECT_TIMEOUT_S", raising=False)

    assert ibkr_connect_timeout_seconds() == 20


def test_live_ibkr_connect_timeout_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("ETA_IBKR_CONNECT_TIMEOUT_S", "18")

    assert ibkr_connect_timeout_seconds() == 18


def test_live_ibkr_connect_timeout_invalid_env_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("ETA_IBKR_CONNECT_TIMEOUT_S", "bad")

    assert ibkr_connect_timeout_seconds() == 20
