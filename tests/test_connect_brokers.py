from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eta_engine.scripts import connect_brokers
from eta_engine.scripts import workspace_roots
from eta_engine.venues.base import ConnectionStatus, VenueConnectionReport
from eta_engine.venues.connection import BrokerConnectionSummary


def _summary(*, ok: bool, brokers: list[str]) -> BrokerConnectionSummary:
    status = ConnectionStatus.READY if ok else ConnectionStatus.FAILED
    return BrokerConnectionSummary(
        generated_at_utc=datetime(2026, 5, 16, tzinfo=UTC),
        configured_brokers=brokers,
        config_path="C:/EvolutionaryTradingAlgo/eta_engine/config.json",
        reports=[
            VenueConnectionReport(
                venue=broker,
                status=status,
                creds_present=True,
                error=None if ok else "probe failed",
            )
            for broker in brokers
        ],
    )


def test_connect_brokers_defaults_to_canonical_runtime_report_dir() -> None:
    parser = connect_brokers._build_parser()
    args = parser.parse_args([])

    assert connect_brokers.DEFAULT_OUT_DIR == workspace_roots.ETA_BROKER_CONNECTION_REPORT_DIR
    assert args.out_dir == workspace_roots.ETA_BROKER_CONNECTION_REPORT_DIR


def test_connect_brokers_default_probe_writes_report_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: dict[str, object] = {}

    class _FakeManager:
        def __init__(self, summary: BrokerConnectionSummary) -> None:
            self._summary = summary

        async def connect(self, names: list[str] | None = None) -> BrokerConnectionSummary:
            seen["names"] = names
            return self._summary

    class _FakeManagerFactory:
        @classmethod
        def from_env(
            cls,
            *,
            config_path: Path,
            bybit_testnet: bool | None = None,
            tradovate_demo: bool | None = None,
        ) -> _FakeManager:
            seen["config_path"] = config_path
            seen["bybit_testnet"] = bybit_testnet
            seen["tradovate_demo"] = tradovate_demo
            return _FakeManager(_summary(ok=True, brokers=["ibkr", "tastytrade"]))

    monkeypatch.setattr(connect_brokers, "BrokerConnectionManager", _FakeManagerFactory)

    config_path = tmp_path / "config.json"
    out_dir = tmp_path / "reports"

    rc = connect_brokers.main(
        [
            "--config",
            str(config_path),
            "--out-dir",
            str(out_dir),
        ]
    )

    latest = out_dir / "broker_connections_latest.json"
    payload = json.loads(latest.read_text(encoding="utf-8"))

    assert rc == 0
    assert seen["names"] is None
    assert seen["config_path"] == config_path
    assert seen["bybit_testnet"] is None
    assert seen["tradovate_demo"] is None
    assert payload["configured_brokers"] == ["ibkr", "tastytrade"]
    assert payload["source"] == "broker_connect"
    assert payload["summary"]["overall_ok"] is True


def test_connect_brokers_reconnect_limits_scope_and_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, object] = {}

    class _FakeManager:
        def __init__(self, summary: BrokerConnectionSummary) -> None:
            self._summary = summary

        async def connect(self, names: list[str] | None = None) -> BrokerConnectionSummary:
            seen["names"] = names
            return self._summary

    class _FakeManagerFactory:
        @classmethod
        def from_env(
            cls,
            *,
            config_path: Path,
            bybit_testnet: bool | None = None,
            tradovate_demo: bool | None = None,
        ) -> _FakeManager:
            seen["config_path"] = config_path
            return _FakeManager(_summary(ok=False, brokers=["ibkr"]))

    monkeypatch.setattr(connect_brokers, "BrokerConnectionManager", _FakeManagerFactory)

    rc = connect_brokers.main(
        [
            "--reconnect",
            "ibkr",
            "--json",
            "--out-dir",
            str(tmp_path / "reports"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert seen["names"] == ["ibkr"]
    assert payload["configured_brokers"] == ["ibkr"]
    assert payload["source"] == "broker_reconnect"
    assert payload["summary"]["overall_ok"] is False


def test_connect_brokers_rejects_reconnect_plus_brokers() -> None:
    with pytest.raises(SystemExit) as excinfo:
        connect_brokers.main(
            [
                "--reconnect",
                "ibkr",
                "--brokers",
                "tastytrade",
            ]
        )

    assert excinfo.value.code == 2
