"""
Fixture-replay tests for IBKR + Tastytrade ``get_net_liquidation`` parsers.

M4 closure (v0.1.68). The Red Team review of v0.1.63 R1 flagged that
the broker payload parsers (``IbkrClientPortalVenue.get_balance`` /
``TastytradeVenue.get_balance``) had no recorded fixtures -- so a
broker-side schema change (a renamed key, a wrapper layer added, a
field's string -> float type flip) would silently break parsing.
The drift detector would then receive ``None`` from
``get_net_liquidation()`` on every poll and classify every tick as
``no_broker_data`` -- effectively disabling drift detection without
any obvious failure mode.

This module replays canonical recorded payloads through the parser
and asserts the parsed value matches the fixture's
``expected_net_liquidation``. A future change to the broker schema
that we do not adapt for surfaces as a hard test failure rather than
a silent live regression.

Each fixture also has a couple of *negative* cases derived from it:

  * ``net_liquidation`` field removed -> parser returns ``None``
    (degraded broker data path).
  * ``net_liquidation`` field is not numeric -> parser returns
    ``None`` (defensive coercion).
  * Wrapper layer (``data`` for Tasty, top-level for IBKR) is
    missing or wrong type -> parser returns ``None``.

The negative cases assert the "MUST NOT raise" guarantee of the
:class:`BrokerEquityAdapter` Protocol holds even on malformed input.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "broker_payloads"


def _load_fixture(name: str) -> dict:
    """Load a fixture JSON, drop the ``_provenance`` documentation key."""
    raw = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    raw.pop("_provenance", None)
    return raw


def _expected_net_liq(name: str) -> float:
    """Read the expected net-liq value from the fixture's provenance block."""
    raw = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return float(raw["_provenance"]["expected_net_liquidation"])


# ---------------------------------------------------------------------------
# IBKR
# ---------------------------------------------------------------------------


def _make_ibkr_with_payload(payload: dict | None):
    """Construct an IbkrClientPortalVenue with ``_get`` patched to return payload.

    Sets credentials to a present-but-fake state so ``has_credentials()``
    returns True and ``get_balance`` proceeds to call ``_get``.
    """
    from eta_engine.venues.ibkr import (
        IbkrClientPortalConfig,
        IbkrClientPortalVenue,
    )

    cfg = IbkrClientPortalConfig(
        base_url="https://localhost:5000/v1/api",
        account_id="DU1234567",
    )
    venue = IbkrClientPortalVenue(config=cfg)
    # Patch the HTTP layer to return the canned fixture (or whatever
    # payload the test passes -- including None for HTTP-failure paths).

    async def _stub_get(_path: str) -> dict | None:
        return payload

    venue._get = _stub_get  # type: ignore[method-assign]  # noqa: SLF001
    return venue


class TestIbkrFixtureReplay:
    """v0.1.68 M4 -- IBKR Client Portal /portfolio/{id}/summary parser."""

    @pytest.mark.asyncio
    async def test_canonical_fixture_parses_to_expected_net_liq(self) -> None:
        payload = _load_fixture("ibkr_account_summary.json")
        expected = _expected_net_liq("ibkr_account_summary.json")
        venue = _make_ibkr_with_payload(payload)
        assert await venue.get_net_liquidation() == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_canonical_fixture_populates_all_balance_fields(self) -> None:
        payload = _load_fixture("ibkr_account_summary.json")
        venue = _make_ibkr_with_payload(payload)
        balance = await venue.get_balance()
        # Every field the parser asks for should land in the dict.
        assert "net_liquidation" in balance
        assert "equity_with_loan" in balance
        assert "total_cash" in balance
        assert "available_funds" in balance

    @pytest.mark.asyncio
    async def test_missing_netliquidation_key_returns_none(self) -> None:
        payload = _load_fixture("ibkr_account_summary.json")
        del payload["netliquidation"]
        venue = _make_ibkr_with_payload(payload)
        assert await venue.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_amount_subkey_missing_treated_as_plain_field(self) -> None:
        """If the .amount drill-down key is gone but the field is plain, parse it."""
        payload = _load_fixture("ibkr_account_summary.json")
        # Replace the {amount: ...} dict with a bare numeric -- the
        # parser handles that path.
        payload["netliquidation"] = 51234.56
        venue = _make_ibkr_with_payload(payload)
        assert await venue.get_net_liquidation() == pytest.approx(51234.56)

    @pytest.mark.asyncio
    async def test_non_numeric_amount_returns_none(self) -> None:
        """A garbage string in .amount should fail coercion to None, not raise."""
        payload = _load_fixture("ibkr_account_summary.json")
        payload["netliquidation"] = {"amount": "not a number"}
        venue = _make_ibkr_with_payload(payload)
        assert await venue.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_http_returns_none_yields_none(self) -> None:
        """HTTP error / timeout / no-creds path: get_balance() returns {}."""
        venue = _make_ibkr_with_payload(payload=None)
        assert await venue.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_http_returns_non_dict_yields_none(self) -> None:
        """A list / str / int from the server: parser must defensively bail."""
        venue = _make_ibkr_with_payload(payload=["not", "a", "dict"])  # type: ignore[arg-type]
        assert await venue.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_no_credentials_yields_none(self) -> None:
        """has_credentials() False -> get_balance returns {} -> None."""
        from eta_engine.venues.ibkr import (
            IbkrClientPortalConfig,
            IbkrClientPortalVenue,
        )

        # Empty config -> no creds.
        cfg = IbkrClientPortalConfig(base_url="", account_id="")
        venue = IbkrClientPortalVenue(config=cfg)
        assert venue.has_credentials() is False
        assert await venue.get_net_liquidation() is None


# ---------------------------------------------------------------------------
# Tastytrade
# ---------------------------------------------------------------------------


def _make_tasty_with_payload(payload: dict | None):
    """Construct a TastytradeVenue with ``_get`` patched + fake credentials."""
    from eta_engine.venues.tastytrade import (
        TastytradeConfig,
        TastytradeVenue,
    )

    cfg = TastytradeConfig(
        base_url="https://api.cert.tastyworks.com",
        account_number="5WT12345",
        session_token="fake-token-for-tests",
    )
    venue = TastytradeVenue(config=cfg)

    async def _stub_get(_path: str) -> dict | None:
        return payload

    venue._get = _stub_get  # type: ignore[method-assign]  # noqa: SLF001
    return venue


class TestTastyFixtureReplay:
    """v0.1.68 M4 -- Tastytrade /accounts/{id}/balances parser."""

    @pytest.mark.asyncio
    async def test_canonical_fixture_parses_to_expected_net_liq(self) -> None:
        payload = _load_fixture("tastytrade_balance.json")
        expected = _expected_net_liq("tastytrade_balance.json")
        venue = _make_tasty_with_payload(payload)
        assert await venue.get_net_liquidation() == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_canonical_fixture_populates_balance_fields(self) -> None:
        payload = _load_fixture("tastytrade_balance.json")
        venue = _make_tasty_with_payload(payload)
        balance = await venue.get_balance()
        # The keys land hyphen->underscore translated.
        assert "net_liquidating_value" in balance
        assert "cash_balance" in balance
        assert "equity_buying_power" in balance

    @pytest.mark.asyncio
    async def test_missing_data_wrapper_returns_none(self) -> None:
        """Tasty wraps every payload in {"data": {...}}; absence of the
        wrapper means the response is malformed and we cannot trust it."""
        payload = _load_fixture("tastytrade_balance.json")
        del payload["data"]
        venue = _make_tasty_with_payload(payload)
        assert await venue.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_data_wrapper_not_a_dict_returns_none(self) -> None:
        """If the data key is a list / str / null, defensive bail to None."""
        payload = _load_fixture("tastytrade_balance.json")
        payload["data"] = "unexpected string"
        venue = _make_tasty_with_payload(payload)
        assert await venue.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_missing_netliquidating_key_returns_none(self) -> None:
        payload = copy.deepcopy(_load_fixture("tastytrade_balance.json"))
        del payload["data"]["net-liquidating-value"]
        venue = _make_tasty_with_payload(payload)
        assert await venue.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_string_typed_value_coerces_to_float(self) -> None:
        """Tasty ships decimals as strings. The float() coercion is the
        whole point of this contract -- pin it."""
        payload = copy.deepcopy(_load_fixture("tastytrade_balance.json"))
        payload["data"]["net-liquidating-value"] = "12345.67"
        venue = _make_tasty_with_payload(payload)
        assert await venue.get_net_liquidation() == pytest.approx(12345.67)

    @pytest.mark.asyncio
    async def test_non_numeric_value_returns_none(self) -> None:
        payload = copy.deepcopy(_load_fixture("tastytrade_balance.json"))
        payload["data"]["net-liquidating-value"] = "not a number"
        venue = _make_tasty_with_payload(payload)
        assert await venue.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_http_returns_none_yields_none(self) -> None:
        venue = _make_tasty_with_payload(payload=None)
        assert await venue.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_no_credentials_yields_none(self) -> None:
        from eta_engine.venues.tastytrade import (
            TastytradeConfig,
            TastytradeVenue,
        )

        cfg = TastytradeConfig(base_url="", account_number="", session_token="")
        venue = TastytradeVenue(config=cfg)
        assert venue.has_credentials() is False
        assert await venue.get_net_liquidation() is None


# ---------------------------------------------------------------------------
# Cross-venue protocol fit
# ---------------------------------------------------------------------------


class TestParsersStillSatisfyProtocol:
    """v0.1.68 M4 -- the parser-touched venues still satisfy the protocol.

    Locks down a subtle regression risk: a future schema-fix that
    accidentally renames ``get_net_liquidation`` to e.g.
    ``get_net_liq`` would silently break the
    :class:`BrokerEquityAdapter` Protocol surface. The test below
    is a structural pin that catches that.
    """

    def test_ibkr_satisfies_protocol(self) -> None:
        from eta_engine.core.broker_equity_adapter import (
            BrokerEquityAdapter,
        )
        from eta_engine.venues.ibkr import IbkrClientPortalVenue

        assert isinstance(IbkrClientPortalVenue(), BrokerEquityAdapter)

    def test_tastytrade_satisfies_protocol(self) -> None:
        from eta_engine.core.broker_equity_adapter import (
            BrokerEquityAdapter,
        )
        from eta_engine.venues.tastytrade import TastytradeVenue

        assert isinstance(TastytradeVenue(), BrokerEquityAdapter)
