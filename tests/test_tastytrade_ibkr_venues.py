"""Tests for Tastytrade and IBKR paper venue adapters."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from eta_engine.venues import (
    ConnectionStatus,
    IbkrClientPortalConfig,
    IbkrClientPortalVenue,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TastytradeConfig,
    TastytradeVenue,
    ibkr_paper_readiness,
    tastytrade_paper_readiness,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_tastytrade_config_reads_secret_files(tmp_path: Path) -> None:
    account_file = tmp_path / "tasty_account.txt"
    token_file = tmp_path / "tasty_token.txt"
    account_file.write_text("5WT12345\n", encoding="utf-8")
    token_file.write_text("session-token\n", encoding="utf-8")

    config = TastytradeConfig.from_env(
        {
            "TASTY_ACCOUNT_NUMBER_FILE": str(account_file),
            "TASTY_SESSION_TOKEN_FILE": str(token_file),
        },
    )

    assert config.account_number == "5WT12345"
    assert config.session_token == "session-token"
    assert config.missing_requirements() == []


def test_tastytrade_config_reads_broker_paper_env_file(tmp_path: Path) -> None:
    account_file = tmp_path / "tasty_account.txt"
    token_file = tmp_path / "tasty_token.txt"
    broker_env = tmp_path / "broker_paper.env"
    account_file.write_text("5WT12345\n", encoding="utf-8")
    token_file.write_text("session-token\n", encoding="utf-8")
    broker_env.write_text(
        "\n".join(
            [
                f"TASTY_ACCOUNT_NUMBER_FILE={account_file}",
                f"TASTY_SESSION_TOKEN_FILE={token_file}",
            ],
        ),
        encoding="utf-8",
    )

    config = TastytradeConfig.from_env({"FIRM_BROKER_PAPER_ENV_FILE": str(broker_env)})

    assert config.account_number == "5WT12345"
    assert config.session_token == "session-token"
    assert config.missing_requirements() == []


def test_tastytrade_config_reads_mnq_runtime_secret_defaults(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "tastytrade_account_number.txt").write_text("5WT12345\n", encoding="utf-8")
    (secrets / "tastytrade_session_token.txt").write_text("runtime-token\n", encoding="utf-8")

    config = TastytradeConfig.from_env({"APEX_RUNTIME_ROOT": str(tmp_path)})

    assert config.account_number == "5WT12345"
    assert config.session_token == "runtime-token"
    assert config.missing_requirements() == []


def test_tastytrade_process_env_wins_over_mnq_runtime_env_file(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    broker_env = secrets / "broker_paper.env"
    broker_env.write_text(
        "\n".join(
            [
                "TASTY_ACCOUNT_NUMBER=from-file",
                "TASTY_SESSION_TOKEN=from-file-token",
            ],
        ),
        encoding="utf-8",
    )

    config = TastytradeConfig.from_env(
        {
            "APEX_RUNTIME_ROOT": str(tmp_path),
            "TASTY_ACCOUNT_NUMBER": "from-env",
            "TASTY_SESSION_TOKEN": "from-env-token",
        },
    )

    assert config.account_number == "from-env"
    assert config.session_token == "from-env-token"
    assert config.missing_requirements() == []


def test_tastytrade_readiness_and_payload_are_paper_safe() -> None:
    env = {
        "TASTY_ACCOUNT_NUMBER": "5WT12345",
        "TASTY_SESSION_TOKEN": "token",
    }
    venue = TastytradeVenue(TastytradeConfig.from_env(env))
    req = OrderRequest(
        symbol="MNQM6",
        side=Side.BUY,
        qty=1,
        order_type=OrderType.LIMIT,
        price=21000.25,
        client_order_id="cid-1",
    )

    readiness = tastytrade_paper_readiness(env)
    payload = venue.build_order_payload(req)
    result = asyncio.run(venue.place_order(req))

    assert readiness["ready"] is True
    assert payload["client-order-id"] == "cid-1"
    assert payload["order-type"] == "Limit"
    assert payload["legs"][0]["symbol"] == "/MNQM6"
    assert result.status is OrderStatus.OPEN
    assert result.raw["venue"] == "tastytrade"


def test_tastytrade_connect_reports_stub_without_configuration() -> None:
    report = asyncio.run(TastytradeVenue(TastytradeConfig()).connect())

    assert report.venue == "tastytrade"
    assert report.status is ConnectionStatus.STUBBED
    assert report.details["mode"] == "paper"
    assert "TASTY_ACCOUNT_NUMBER" in report.details["missing"]


def test_ibkr_config_reads_secret_files_and_conids(tmp_path: Path) -> None:
    account_file = tmp_path / "ibkr_account.txt"
    conid_file = tmp_path / "ibkr_conids.json"
    account_file.write_text("DU123456\n", encoding="utf-8")
    conid_file.write_text(json.dumps({"MNQM6": 123456789}), encoding="utf-8")

    config = IbkrClientPortalConfig.from_env(
        {
            "IBKR_ACCOUNT_ID_FILE": str(account_file),
            "IBKR_SYMBOL_CONID_MAP_FILE": str(conid_file),
        },
    )

    assert config.account_id == "DU123456"
    assert config.conid_for("MNQM6") == 123456789
    assert config.missing_requirements() == []


def test_ibkr_config_reads_broker_paper_env_file(tmp_path: Path) -> None:
    account_file = tmp_path / "ibkr_account.txt"
    conid_file = tmp_path / "ibkr_conids.json"
    broker_env = tmp_path / "broker_paper.env"
    account_file.write_text("DU123456\n", encoding="utf-8")
    conid_file.write_text(json.dumps({"MNQM6": 123456789}), encoding="utf-8")
    broker_env.write_text(
        "\n".join(
            [
                f"IBKR_ACCOUNT_ID_FILE={account_file}",
                f"IBKR_SYMBOL_CONID_MAP_FILE={conid_file}",
            ],
        ),
        encoding="utf-8",
    )

    config = IbkrClientPortalConfig.from_env({"FIRM_BROKER_PAPER_ENV_FILE": str(broker_env)})

    assert config.account_id == "DU123456"
    assert config.conid_for("MNQM6") == 123456789
    assert config.missing_requirements() == []


def test_ibkr_config_reads_mnq_runtime_secret_defaults(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "ibkr_account_id.txt").write_text("DU123456\n", encoding="utf-8")
    (secrets / "ibkr_symbol_conids.json").write_text(
        json.dumps({"MNQM6": 123456789}),
        encoding="utf-8",
    )

    config = IbkrClientPortalConfig.from_env({"APEX_RUNTIME_ROOT": str(tmp_path)})

    assert config.account_id == "DU123456"
    assert config.conid_for("MNQM6") == 123456789
    assert config.missing_requirements() == []


def test_ibkr_process_env_wins_over_mnq_runtime_env_file(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    broker_env = secrets / "broker_paper.env"
    broker_env.write_text(
        "\n".join(
            [
                "IBKR_ACCOUNT_ID=DU999999",
                f"IBKR_SYMBOL_CONID_MAP={json.dumps({'MNQM6': 999999999})}",
            ],
        ),
        encoding="utf-8",
    )

    config = IbkrClientPortalConfig.from_env(
        {
            "APEX_RUNTIME_ROOT": str(tmp_path),
            "IBKR_ACCOUNT_ID": "DU123456",
            "IBKR_SYMBOL_CONID_MAP": json.dumps({"MNQM6": 123456789}),
        },
    )

    assert config.account_id == "DU123456"
    assert config.conid_for("MNQM6") == 123456789
    assert config.missing_requirements() == []


def test_ibkr_readiness_and_payload_are_paper_safe() -> None:
    env = {
        "IBKR_ACCOUNT_ID": "DU123456",
        "IBKR_SYMBOL_CONID_MAP": json.dumps({"MNQM6": 123456789}),
    }
    venue = IbkrClientPortalVenue(IbkrClientPortalConfig.from_env(env))
    req = OrderRequest(
        symbol="MNQM6",
        side=Side.SELL,
        qty=2,
        order_type=OrderType.LIMIT,
        price=21000.25,
        client_order_id="cid-2",
    )

    readiness = ibkr_paper_readiness(env)
    payload = venue.build_order_payload(req, conid=123456789)
    result = asyncio.run(venue.place_order(req))

    assert readiness["ready"] is True
    assert payload["acctId"] == "DU123456"
    assert payload["conid"] == 123456789
    assert payload["orderType"] == "LMT"
    assert payload["side"] == "SELL"
    assert result.status is OrderStatus.OPEN
    assert result.raw["venue"] == "ibkr"


def test_ibkr_connect_reports_stub_without_configuration() -> None:
    report = asyncio.run(IbkrClientPortalVenue(IbkrClientPortalConfig()).connect())

    assert report.venue == "ibkr"
    assert report.status is ConnectionStatus.STUBBED
    assert report.details["mode"] == "paper"
    assert "IBKR_ACCOUNT_ID" in report.details["missing"]


def test_ibkr_requires_du_paper_account() -> None:
    readiness = ibkr_paper_readiness(
        {
            "IBKR_ACCOUNT_ID": "U123456",
            "IBKR_SYMBOL_CONID_MAP": json.dumps({"MNQM6": 123456789}),
        },
    )

    assert readiness["ready"] is False
    assert "IBKR_ACCOUNT_ID must be a paper account id beginning with DU" in readiness["missing"]


# ---------------------------------------------------------------------------
# R1 -- broker-side MTM equity readers
# ---------------------------------------------------------------------------


def test_tastytrade_get_net_liquidation_without_credentials_is_none() -> None:
    venue = TastytradeVenue(TastytradeConfig())
    assert asyncio.run(venue.get_net_liquidation()) is None


def test_tastytrade_get_net_liquidation_parses_balance(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    venue = TastytradeVenue(
        TastytradeConfig(
            base_url="https://api.cert.tastyworks.com",
            account_number="5WT12345",
            session_token="tok",
        ),
    )

    async def fake_get(path: str) -> dict[str, object]:
        assert "/balances" in path
        return {
            "data": {
                "cash-balance": "10000.00",
                "equity-buying-power": "30000.00",
                "net-liquidating-value": "50123.45",
            },
        }

    monkeypatch.setattr(venue, "_get", fake_get)
    assert asyncio.run(venue.get_net_liquidation()) == 50_123.45


def test_tastytrade_get_net_liquidation_missing_field_returns_none(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    venue = TastytradeVenue(
        TastytradeConfig(
            base_url="https://api.cert.tastyworks.com",
            account_number="5WT12345",
            session_token="tok",
        ),
    )

    async def fake_get(path: str) -> dict[str, object]:
        assert "/balances" in path
        return {"data": {"cash-balance": "10000.00"}}

    monkeypatch.setattr(venue, "_get", fake_get)
    assert asyncio.run(venue.get_net_liquidation()) is None


def test_tastytrade_get_net_liquidation_malformed_returns_none(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    venue = TastytradeVenue(
        TastytradeConfig(
            base_url="https://api.cert.tastyworks.com",
            account_number="5WT12345",
            session_token="tok",
        ),
    )

    async def fake_get(path: str) -> dict[str, object]:
        return {"data": {"net-liquidating-value": "not-a-number"}}

    monkeypatch.setattr(venue, "_get", fake_get)
    assert asyncio.run(venue.get_net_liquidation()) is None


def test_ibkr_get_balance_without_credentials_is_empty() -> None:
    venue = IbkrClientPortalVenue(IbkrClientPortalConfig())
    assert asyncio.run(venue.get_balance()) == {}


def test_ibkr_get_net_liquidation_without_credentials_is_none() -> None:
    venue = IbkrClientPortalVenue(IbkrClientPortalConfig())
    assert asyncio.run(venue.get_net_liquidation()) is None


def test_ibkr_get_balance_parses_portfolio_summary(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    venue = IbkrClientPortalVenue(
        IbkrClientPortalConfig(
            account_id="DU123456",
            symbol_conids={"MNQM6": 123456789},
        ),
    )

    async def fake_get(path: str) -> dict[str, object]:
        assert "/portfolio/DU123456/summary" in path
        return {
            "netliquidation": {"amount": 50_123.45, "currency": "USD"},
            "equitywithloanvalue": {"amount": 50_100.00, "currency": "USD"},
            "totalcashvalue": {"amount": 10_000.00, "currency": "USD"},
            "availablefunds": {"amount": 9_800.00, "currency": "USD"},
        }

    monkeypatch.setattr(venue, "_get", fake_get)
    balance = asyncio.run(venue.get_balance())
    assert balance["net_liquidation"] == 50_123.45
    assert balance["equity_with_loan"] == 50_100.00
    assert balance["total_cash"] == 10_000.00
    assert balance["available_funds"] == 9_800.00


def test_ibkr_get_net_liquidation_returns_net_liq(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    venue = IbkrClientPortalVenue(
        IbkrClientPortalConfig(
            account_id="DU123456",
            symbol_conids={"MNQM6": 123456789},
        ),
    )

    async def fake_get(path: str) -> dict[str, object]:
        return {"netliquidation": {"amount": "49999.99"}}

    monkeypatch.setattr(venue, "_get", fake_get)
    assert asyncio.run(venue.get_net_liquidation()) == 49_999.99


def test_ibkr_get_balance_handles_malformed_response(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    venue = IbkrClientPortalVenue(
        IbkrClientPortalConfig(
            account_id="DU123456",
            symbol_conids={"MNQM6": 123456789},
        ),
    )

    async def fake_get(path: str) -> None:
        return None

    monkeypatch.setattr(venue, "_get", fake_get)
    assert asyncio.run(venue.get_balance()) == {}
    assert asyncio.run(venue.get_net_liquidation()) is None
