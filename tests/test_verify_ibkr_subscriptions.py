"""Tests for IBKR subscription verifier setup/readiness handling."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.scripts import verify_ibkr_subscriptions as vis

if TYPE_CHECKING:
    import pytest


def test_ensure_asyncio_event_loop_creates_loop_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = object()
    captured = {}

    def missing_loop() -> object:
        raise RuntimeError("There is no current event loop in thread 'MainThread'.")

    monkeypatch.setattr(vis.asyncio, "get_event_loop", missing_loop)
    monkeypatch.setattr(vis.asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(vis.asyncio, "set_event_loop", lambda value: captured.setdefault("loop", value))

    vis._ensure_asyncio_event_loop()

    assert captured["loop"] is loop


def test_ensure_asyncio_event_loop_replaces_closed_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    replacement = object()
    captured = {}

    class ClosedLoop:
        def is_closed(self) -> bool:
            return True

    monkeypatch.setattr(vis.asyncio, "get_event_loop", lambda: ClosedLoop())
    monkeypatch.setattr(vis.asyncio, "new_event_loop", lambda: replacement)
    monkeypatch.setattr(vis.asyncio, "set_event_loop", lambda value: captured.setdefault("loop", value))

    vis._ensure_asyncio_event_loop()

    assert captured["loop"] is replacement


def test_probe_order_api_access_detects_read_only_gateway() -> None:
    class Event:
        def __init__(self) -> None:
            self.handlers: list[Callable[..., None]] = []

        def __iadd__(self, handler: Callable[..., None]) -> Event:
            self.handlers.append(handler)
            return self

        def __isub__(self, handler: Callable[..., None]) -> Event:
            self.handlers.remove(handler)
            return self

        def emit(self, *args: object) -> None:
            for handler in list(self.handlers):
                handler(*args)

    class FakeIB:
        def __init__(self) -> None:
            self.errorEvent = Event()

        def reqOpenOrders(self) -> None:
            self.errorEvent.emit(
                -1,
                321,
                "Error validating request: The API interface is currently in Read-Only mode.",
                None,
            )

        def sleep(self, _seconds: float) -> None:
            return None

    status = vis._probe_order_api_access(FakeIB())

    assert status["ready"] is False
    assert status["status"] == "read_only"
    assert "Read-Only mode" in status["detail"]


def test_ibc_credential_status_flags_placeholder_password(tmp_path: Path) -> None:
    password_file = tmp_path / "ibkr_pw.txt"
    credential_json = tmp_path / "ibkr_credentials.json"
    password_file.write_text("local-only-placeholder\n", encoding="utf-8")
    credential_json.write_text(json.dumps({"username": "paper_user"}), encoding="utf-8-sig")

    status = vis._ibc_credential_status(
        {},
        password_file=password_file,
        credential_json=credential_json,
        ibc_private_config=None,
        ibc_password_files=(),
    )

    assert status["ready"] is False
    assert status["status"] == "PLACEHOLDER_PASSWORD"
    assert status["login_present"] is True
    assert status["password_present"] is False
    assert status["password_file_placeholder"] is True
    assert "operator_action" in status


def test_ibc_credential_status_prefers_usable_env_password(tmp_path: Path) -> None:
    password_file = tmp_path / "ibkr_pw.txt"
    credential_json = tmp_path / "ibkr_credentials.json"
    password_file.write_text("PLACEHOLDER_PASSWORD\n", encoding="utf-8")
    credential_json.write_text(json.dumps({"username": "paper_user"}), encoding="utf-8")

    status = vis._ibc_credential_status(
        {"ETA_IBC_PASSWORD": "unit-test-usable-value"},
        password_file=password_file,
        credential_json=credential_json,
        ibc_private_config=None,
        ibc_password_files=(),
    )

    assert status["ready"] is True
    assert status["status"] == "READY"
    assert status["password_source"] == "env"
    assert status["password_file_placeholder"] is True
    assert status["operator_action"] is None


def test_ibc_credential_status_accepts_private_ibc_config(tmp_path: Path) -> None:
    password_file = tmp_path / "ibkr_pw.txt"
    credential_json = tmp_path / "ibkr_credentials.json"
    private_config = tmp_path / "private" / "config.ini"
    password_file.write_text("PLACEHOLDER_PASSWORD\n", encoding="utf-8")
    credential_json.write_text(json.dumps({}), encoding="utf-8")
    private_config.parent.mkdir(parents=True)
    private_config.write_text(
        "IbLoginId=paper_user\nIbPassword=unit-test-private-password\n",
        encoding="utf-8",
    )

    status = vis._ibc_credential_status(
        {},
        password_file=password_file,
        credential_json=credential_json,
        ibc_private_config=private_config,
        ibc_password_files=(),
    )

    assert status["ready"] is True
    assert status["status"] == "READY"
    assert status["login_source"] == "ibc_private_config"
    assert status["password_source"] == "ibc_private_config"
    assert status["ibc_private_config_exists"] is True
    assert status["operator_action"] is None


def test_main_reports_gateway_unreachable_when_credentials_are_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_log = tmp_path / "ibkr_subscription_status.jsonl"
    credential_status = {
        "ready": True,
        "status": "READY",
        "login_present": True,
        "password_present": True,
        "password_source": "ibc_private_config",
        "operator_action": None,
    }
    monkeypatch.setattr(vis, "STATUS_LOG", status_log)
    monkeypatch.setattr(vis, "_tws_port", lambda: None)
    monkeypatch.setattr(vis, "_ibc_credential_status", lambda: credential_status)
    monkeypatch.setattr(sys, "argv", ["verify_ibkr_subscriptions", "--json"])

    rc = vis.main()

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["setup_status"] == "BLOCKED"
    assert payload["setup_error_code"] == "gateway_unreachable"
    assert payload["credential_status"]["status"] == "READY"
    assert payload["operator_action"] == "Start IB Gateway or run ETA-IBGateway-RunNow."


def test_main_persists_blocked_setup_when_gateway_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_log = tmp_path / "ibkr_subscription_status.jsonl"
    credential_status = {
        "ready": False,
        "status": "PLACEHOLDER_PASSWORD",
        "login_present": True,
        "password_present": False,
        "password_source": None,
        "operator_action": "Seed the protected password file.",
    }
    monkeypatch.setattr(vis, "STATUS_LOG", status_log)
    monkeypatch.setattr(vis, "_tws_port", lambda: None)
    monkeypatch.setattr(vis, "_ibc_credential_status", lambda: credential_status)
    monkeypatch.setattr(sys, "argv", ["verify_ibkr_subscriptions", "--json"])

    rc = vis.main()

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["setup_status"] == "BLOCKED"
    assert payload["setup_error_code"] == "ibc_credentials_missing"
    assert payload["credential_status"]["status"] == "PLACEHOLDER_PASSWORD"
    persisted = json.loads(status_log.read_text(encoding="utf-8").splitlines()[-1])
    assert persisted["setup_status"] == "BLOCKED"
    assert persisted["operator_action"] == "Seed the protected password file."
