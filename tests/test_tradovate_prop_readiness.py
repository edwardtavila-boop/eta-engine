"""Tests for the no-order Tradovate prop readiness checklist."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.scripts import tradovate_prop_readiness as tpr

if TYPE_CHECKING:
    import pytest


def _patch_secrets(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    def fake_get(key: str, required: bool = False) -> str | None:  # noqa: ARG001
        return values.get(key)

    monkeypatch.setattr(tpr.SECRETS, "get", fake_get)


def _write_routing_config(path: Path, active: bool = False) -> None:
    bot_line = (
        "  volume_profile_mnq: { venue: tradovate, account_alias: blusky_50k }\n"
        if active
        else "  # volume_profile_mnq: { venue: tradovate, account_alias: blusky_50k }\n"
    )
    path.write_text(
        """
version: 2
prop_accounts:
  blusky_50k:
    venue: tradovate
    env: demo
    account_id_env: BLUSKY_TRADOVATE_ACCOUNT_ID
    creds_env_prefix: BLUSKY_
bots:
"""
        + bot_line,
        encoding="utf-8",
    )


def test_predeposit_ready_with_prop_login_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_secrets(
        monkeypatch,
        {
            "BLUSKY_TRADOVATE_USERNAME": "BSK_user",
            "BLUSKY_TRADOVATE_PASSWORD": "pw",
        },
    )
    routing = tmp_path / "bot_broker_routing.yaml"
    _write_routing_config(routing)

    report = tpr.build_report(
        prop_account="blusky_50k",
        phase="predeposit",
        routing_config=routing,
        auth_status=tmp_path / "missing_auth.json",
    )

    assert report["summary"] == "READY_FOR_DEPOSIT"
    assert tpr.exit_code(report) == 0
    assert report["secret_presence"]["present"] == [
        "BLUSKY_TRADOVATE_USERNAME",
        "BLUSKY_TRADOVATE_PASSWORD",
    ]
    assert any(check["name"] == "prop_login_credentials" and check["status"] == "PASS" for check in report["checks"])
    assert any(check["name"] == "prop_api_credentials" and check["status"] == "WAIT" for check in report["checks"])


def test_cutover_blocks_until_api_credentials_and_authorized_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_secrets(
        monkeypatch,
        {
            "BLUSKY_TRADOVATE_USERNAME": "BSK_user",
            "BLUSKY_TRADOVATE_PASSWORD": "pw",
        },
    )
    routing = tmp_path / "bot_broker_routing.yaml"
    _write_routing_config(routing)

    report = tpr.build_report(
        prop_account="blusky_50k",
        phase="cutover",
        routing_config=routing,
        auth_status=tmp_path / "missing_auth.json",
    )

    assert report["summary"] == "BLOCKED"
    assert tpr.exit_code(report) == 1
    assert any(check["name"] == "prop_api_credentials" and check["status"] == "BLOCKED" for check in report["checks"])
    assert any(check["name"] == "oauth_authorization" and check["status"] == "BLOCKED" for check in report["checks"])


def test_cutover_ready_after_all_credentials_and_authorized_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_secrets(
        monkeypatch,
        {
            "BLUSKY_TRADOVATE_ACCOUNT_ID": "1234567",
            "BLUSKY_TRADOVATE_USERNAME": "BSK_user",
            "BLUSKY_TRADOVATE_PASSWORD": "pw",
            "BLUSKY_TRADOVATE_APP_ID": "EtaEngine",
            "BLUSKY_TRADOVATE_APP_SECRET": "sec",
            "BLUSKY_TRADOVATE_CID": "cid",
        },
    )
    monkeypatch.setenv("ETA_TRADOVATE_ENABLED", "1")
    routing = tmp_path / "bot_broker_routing.yaml"
    _write_routing_config(routing)
    auth_status = tmp_path / "auth.json"
    auth_status.write_text(
        json.dumps(
            {
                "credential_scope": "blusky_50k",
                "demo": True,
                "result": "AUTHORIZED",
                "endpoint": "https://demo.tradovateapi.com/v1",
            },
        ),
        encoding="utf-8",
    )

    report = tpr.build_report(
        prop_account="blusky_50k",
        phase="cutover",
        routing_config=routing,
        auth_status=auth_status,
    )

    assert report["summary"] == "READY_FOR_DRY_RUN"
    assert tpr.exit_code(report) == 0
    assert any(check["name"] == "tradovate_activation_flag" and check["status"] == "PASS" for check in report["checks"])
    assert any(check["name"] == "winning_bot_route" and check["status"] == "SAFE_HELD" for check in report["checks"])
