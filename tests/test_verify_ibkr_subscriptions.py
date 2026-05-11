"""Tests for IBKR subscription verifier setup/readiness handling."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.scripts import verify_ibkr_subscriptions as vis

if TYPE_CHECKING:
    import pytest


def test_ibc_credential_status_flags_placeholder_password(tmp_path: Path) -> None:
    password_file = tmp_path / "ibkr_pw.txt"
    credential_json = tmp_path / "ibkr_credentials.json"
    password_file.write_text("local-only-placeholder\n", encoding="utf-8")
    credential_json.write_text(json.dumps({"username": "paper_user"}), encoding="utf-8-sig")

    status = vis._ibc_credential_status(
        {},
        password_file=password_file,
        credential_json=credential_json,
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
    )

    assert status["ready"] is True
    assert status["status"] == "READY"
    assert status["password_source"] == "env"
    assert status["password_file_placeholder"] is True
    assert status["operator_action"] is None


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
