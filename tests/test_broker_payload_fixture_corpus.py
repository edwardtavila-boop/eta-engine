"""Companion to ``tests/test_broker_payload_fixtures.py``.

The main fixture-replay file (operator's v0.1.68 M4 work) uses
``copy.deepcopy`` + in-test mutation to generate negative cases from
the two canonical fixtures (``ibkr_account_summary.json``,
``tastytrade_balance.json``). That style covers most variations.

This module covers the remaining cases that are easier to express as
**separate fixture files** than as in-test mutations:

* IBKR Client Portal historically alternated between
  ``netliquidation`` (lowercase) and ``netLiquidation`` (camelCase)
  during API revisions. The parser is currently case-sensitive on
  the lowercase form. ``ibkr_summary_camelcase.json`` documents and
  pins what happens when IBKR ships the camelCase variant -- today
  we degrade to ``None`` (drift detection silently disabled). When
  the operator hardens the parser to accept both casings, this test
  changes from "pinning today's brittle behaviour" to "regression
  net for the case-insensitive path."

* IBKR sometimes returns a flat ``"netliquidation": 49850.10`` (no
  ``{"amount": ...}`` wrapper). Parser handles both shapes. The
  flat-amount fixture pins that.

* IBKR returns sentinel strings (``"AccountSecondaryRule"``) when
  a field is structurally present but semantically unavailable.
  Parser must catch the ``ValueError`` from ``float(...)`` and
  degrade to ``None``.

* Tastytrade returns an error envelope (no ``data`` key) when the
  account is not found. Parser must NOT raise.

* Tastytrade may return a literal-zero balance (just-funded paper
  sandbox). Parser must return ``0.0``, NOT ``None`` -- the
  reconciler distinguishes broker-reports-zero from broker-no-data
  by sign of value (``0.0 < min_logical_usd`` triggers the
  no_broker_data branch separately).

Also pins corpus-level invariants:

* Every ``*.json`` file in the fixtures dir is valid JSON.
* The fixtures dir has a ``README.md`` documenting the naming
  convention.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker_payloads"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# IBKR variant fixtures
# ---------------------------------------------------------------------------


def _ibkr_with_payload(payload: dict[str, Any] | None):
    from eta_engine.venues.ibkr import (
        IbkrClientPortalConfig,
        IbkrClientPortalVenue,
    )

    cfg = IbkrClientPortalConfig(
        base_url="https://localhost:5000/v1/api",
        account_id="DU1234567",
    )
    venue = IbkrClientPortalVenue(config=cfg)

    async def _stub_get(_path: str) -> dict[str, Any] | None:
        return payload

    venue._get = _stub_get  # type: ignore[method-assign]  # noqa: SLF001
    return venue


class TestIbkrVariantFixtures:
    @pytest.mark.asyncio
    async def test_flat_amount_fixture_returns_value(self) -> None:
        venue = _ibkr_with_payload(_load("ibkr_summary_flat_amount.json"))
        result = await venue.get_net_liquidation()
        assert result == pytest.approx(49850.10)

    @pytest.mark.asyncio
    async def test_camelcase_fixture_returns_none_documented_breakpoint(
        self,
    ) -> None:
        """REGRESSION PIN: today the parser is case-sensitive on
        ``netliquidation``. If IBKR ever ships a casing change, this
        test will need to be updated -- which is the operator's
        signal to harden the parser."""
        venue = _ibkr_with_payload(_load("ibkr_summary_camelcase.json"))
        result = await venue.get_net_liquidation()
        assert result is None

    @pytest.mark.asyncio
    async def test_wrong_type_string_sentinel_returns_none(self) -> None:
        venue = _ibkr_with_payload(_load("ibkr_summary_wrong_type.json"))
        result = await venue.get_net_liquidation()
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_netliq_returns_none(self) -> None:
        venue = _ibkr_with_payload(_load("ibkr_summary_missing_netliq.json"))
        result = await venue.get_net_liquidation()
        assert result is None


# ---------------------------------------------------------------------------
# Tastytrade variant fixtures
# ---------------------------------------------------------------------------


def _tasty_with_payload(payload: dict[str, Any] | None):
    from eta_engine.venues.tastytrade import (
        TastytradeConfig,
        TastytradeVenue,
    )

    cfg = TastytradeConfig(
        base_url="https://api.cert.tastyworks.com",
        account_number="5WX12345",
        session_token="token",
    )
    venue = TastytradeVenue(config=cfg)

    async def _stub_get(_path: str) -> dict[str, Any] | None:
        return payload

    venue._get = _stub_get  # type: ignore[method-assign]  # noqa: SLF001
    return venue


class TestTastytradeVariantFixtures:
    @pytest.mark.asyncio
    async def test_zero_balance_returns_zero_not_none(self) -> None:
        """A funded-but-empty paper account returns 0.0; reconciler
        treats this differently from no_broker_data (the latter
        skips the comparison entirely; the former triggers a real
        drift check)."""
        venue = _tasty_with_payload(
            _load("tastytrade_balances_zero.json"),
        )
        result = await venue.get_net_liquidation()
        assert result == 0.0
        # And critically, NOT None
        assert result is not None

    @pytest.mark.asyncio
    async def test_no_data_envelope_error_returns_none(self) -> None:
        """When Tastytrade returns ``{"errors": [...]}`` instead of
        ``{"data": {...}}``, the parser's ``isinstance(data, dict)``
        guard kicks in and we degrade to None."""
        venue = _tasty_with_payload(
            _load("tastytrade_balances_no_data_envelope.json"),
        )
        result = await venue.get_net_liquidation()
        assert result is None


# ---------------------------------------------------------------------------
# Corpus integrity
# ---------------------------------------------------------------------------


def test_every_fixture_is_parseable_json() -> None:
    """A JSON parse error in a fixture would surface as a phantom test
    failure that's hard to attribute. Walk the whole corpus up front."""
    for p in FIXTURES.glob("*.json"):
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            pytest.fail(f"fixture {p.name} is not valid JSON: {exc}")


def test_fixtures_dir_has_readme() -> None:
    """Pin the docs alongside the data."""
    assert (FIXTURES / "README.md").exists(), (
        f"missing README in {FIXTURES} -- the fixture-naming convention and provenance rules live there"
    )


def test_no_real_account_id_in_fixtures() -> None:
    """Defensive: scan every fixture for anything that looks like a
    real broker account id pattern.

    IBKR live accounts start with U / I / W (production) or DU
    (paper). Tastytrade accounts are 8-char alphanumerics. This
    test ensures every fixture uses one of the documented synthetic
    placeholders so a future "drop a captured payload" doesn't
    accidentally leak a real id.

    Allow-list the synthetic placeholders below.
    """
    allow = {
        "DU1234567",  # IBKR paper -- this corpus's variant fixtures
        "5WX12345",  # Tastytrade -- this corpus's variant fixtures
        "5WT12345",  # Tastytrade -- canonical operator fixture
    }
    import re

    pat_ibkr = re.compile(r'"DU\d{7,}"|"U\d{7,}"|"I\d{6,}"|"W\d{6,}"')
    pat_tasty = re.compile(r'"[1-9][A-Z]{2}[0-9]{5,}"')
    for p in FIXTURES.glob("*.json"):
        text = p.read_text(encoding="utf-8")
        for hit in pat_ibkr.findall(text) + pat_tasty.findall(text):
            stripped = hit.strip('"')
            assert stripped in allow, (
                f"fixture {p.name} contains a non-allowlisted account "
                f"id-like token {hit!r}. If real, sanitize and replace "
                f"with one of {sorted(allow)}; if synthetic, add to "
                f"the allow-list in this test."
            )
