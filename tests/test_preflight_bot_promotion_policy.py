"""Policy regression tests for scripts.preflight_bot_promotion."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

from eta_engine.scripts import preflight_bot_promotion as mod

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_broker_config_missing_for_crypto_only_checks_ibkr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eta_engine.venues as venues

    class FakeConfig:
        def __init__(self, missing: list[str]) -> None:
            self._missing = missing

        def missing_requirements(self) -> list[str]:
            return self._missing

    class FakeIbkr:
        @classmethod
        def from_env(cls, env: object = None) -> FakeConfig:
            _ = env
            return FakeConfig([])

    class RaisingTastytrade:
        @classmethod
        def from_env(cls, env: object = None) -> FakeConfig:
            _ = env
            raise AssertionError("crypto promotion should not require Tastytrade")

    monkeypatch.setattr(venues, "IbkrClientPortalConfig", FakeIbkr)
    monkeypatch.setattr(venues, "TastytradeConfig", RaisingTastytrade)

    assert mod._broker_config_missing("crypto", {}) == {"IBKR": []}


def test_check_broker_keys_reports_active_futures_brokers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eta_engine.strategies.per_bot_registry as registry

    monkeypatch.setattr(
        registry,
        "get_for_bot",
        lambda bot_id: SimpleNamespace(symbol="MNQM6"),
    )
    monkeypatch.setattr(
        mod,
        "_broker_config_missing",
        lambda venue_class, env: {"IBKR": [], "Tastytrade": []},
    )

    result = mod._check_broker_keys("mnq-live")

    assert result.severity == "green"
    assert result.details["active_brokers"] == ["IBKR", "Tastytrade"]
    assert result.details["dormant_brokers"] == ["Tradovate"]
    assert "Tradovate" not in result.summary


def test_check_broker_keys_missing_does_not_resurrect_tradovate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eta_engine.strategies.per_bot_registry as registry

    monkeypatch.setattr(
        registry,
        "get_for_bot",
        lambda bot_id: SimpleNamespace(symbol="MNQM6"),
    )
    monkeypatch.setattr(
        mod,
        "_broker_config_missing",
        lambda venue_class, env: {
            "IBKR": ["IBKR_ACCOUNT_ID"],
            "Tastytrade": ["TASTY_SESSION_TOKEN"],
        },
    )

    result = mod._check_broker_keys("mnq-live")

    assert result.severity == "red"
    assert "IBKR_ACCOUNT_ID" in result.summary
    assert "TASTY_SESSION_TOKEN" in result.summary
    assert "TRADOVATE_USERNAME" not in result.summary
    assert result.details["dormant_brokers"] == ["Tradovate"]


def test_drift_watchdog_recent_uses_canonical_resolver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log_path = tmp_path / "var" / "eta_engine" / "state" / "drift_watchdog.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text('{"severity":"green"}\n', encoding="utf-8")
    now = datetime.now(UTC).timestamp()

    os.utime(log_path, (now, now))
    monkeypatch.setattr(mod, "default_drift_watchdog_log_path", lambda: log_path)

    result = mod._check_drift_watchdog_recent()

    assert result.severity == "green"
    assert result.details["path"].endswith("drift_watchdog.jsonl")
