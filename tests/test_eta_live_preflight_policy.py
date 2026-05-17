"""Policy regression tests for scripts.eta_live_preflight."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eta_engine.scripts import eta_live_preflight as mod
from eta_engine.scripts import workspace_roots

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_active_broker_requirements_accept_ibkr_and_tastytrade_env() -> None:
    missing = mod._active_broker_missing_requirements(
        {
            "IBKR_ACCOUNT_ID": "DU123456",
            "TASTY_ACCOUNT_NUMBER": "5WT12345",
            "TASTY_SESSION_TOKEN": "session-token",
        },
    )

    assert missing == {"IBKR": [], "Tastytrade": []}


def test_active_broker_requirements_do_not_require_tradovate(
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
            return FakeConfig(["IBKR_ACCOUNT_ID"])

    class FakeTastytrade:
        @classmethod
        def from_env(cls, env: object = None) -> FakeConfig:
            _ = env
            return FakeConfig(["TASTY_ACCOUNT_NUMBER", "TASTY_SESSION_TOKEN"])

    monkeypatch.setattr(venues, "IbkrClientPortalConfig", FakeIbkr)
    monkeypatch.setattr(venues, "TastytradeConfig", FakeTastytrade)

    missing = mod._active_broker_missing_requirements({})
    flat = [item for values in missing.values() for item in values]

    assert "IBKR_ACCOUNT_ID" in flat
    assert "TASTY_ACCOUNT_NUMBER" in flat
    assert "TASTY_SESSION_TOKEN" in flat
    assert "TRADOVATE_USERNAME" not in str(missing)


def test_check_env_files_reports_dormant_tradovate_when_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mod,
        "_active_broker_missing_requirements",
        lambda: {"IBKR": [], "Tastytrade": []},
    )

    result = mod.check_env_files()

    assert result.ok is True
    assert result.name == "env_files"
    assert result.metadata["active_brokers"] == ["IBKR", "Tastytrade"]
    assert result.metadata["dormant_brokers"] == ["Tradovate"]
    assert "Tradovate dormant" in result.detail


def test_kill_switch_paths_are_canonical_workspace_paths() -> None:
    paths = mod._kill_switch_paths()
    rendered = [str(path) for path in paths]

    assert rendered
    assert all(str(mod.WORKSPACE_ROOT) in path for path in rendered)
    assert not any("mnq_data" in path.lower() for path in rendered)
    assert not any("OneDrive" in path for path in rendered)


def test_check_kill_switch_detects_canonical_armed_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(mod, "ROOT", tmp_path / "eta_engine")
    kill_path = tmp_path / "data" / "firm_kill.json"
    kill_path.parent.mkdir(parents=True)
    kill_path.write_text('{"armed": true}', encoding="utf-8")

    result = mod.check_kill_switch()

    assert result.ok is False
    assert result.name == "kill_switch_disarmed"
    assert str(kill_path) in result.detail


def test_check_kaizen_recent_uses_canonical_runtime_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_json = tmp_path / "var" / "eta_engine" / "state" / "kaizen_ledger.json"
    runtime_json.parent.mkdir(parents=True)
    runtime_json.write_text('{"tickets": [], "retrospectives": []}', encoding="utf-8")
    runtime_jsonl = tmp_path / "var" / "eta_engine" / "state" / "kaizen_ledger.jsonl"

    monkeypatch.setattr(workspace_roots, "ETA_KAIZEN_LEDGER_PATH", runtime_json)
    monkeypatch.setattr(workspace_roots, "ETA_KAIZEN_LEDGER_JSONL_PATH", runtime_jsonl)

    result = mod.check_kaizen_recent()

    assert result.name == "kaizen_recent"
    assert result.ok is False
    assert "canonical" in result.detail or "latest retro" in result.detail
