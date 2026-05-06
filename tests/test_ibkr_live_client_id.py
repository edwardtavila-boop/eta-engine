from __future__ import annotations

from eta_engine.venues.ibkr_live import LiveIbkrVenue


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
