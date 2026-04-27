"""Tests for venues.deribit."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from eta_engine.venues.base import OrderRequest, OrderStatus, Side
from eta_engine.venues.deribit import (
    DeribitClient,
    OptionChain,
    OptionContract,
    atm_iv_to_tail_sigma,
)


class FakeDeribitMcp:
    def __init__(
        self,
        instruments: list[dict[str, Any]],
        tickers: list[dict[str, Any]],
        index_price: float,
    ) -> None:
        self._instruments = instruments
        self._tickers = tickers
        self._index_price = index_price

    def get_instruments(self, *, currency: str, kind: str, expired: bool = False):
        return self._instruments

    def get_tickers(self, *, instrument_names: list[str]):
        return [t for t in self._tickers if t["instrument_name"] in instrument_names]

    def get_index_price(self, *, index_name: str):
        return {"index_price": self._index_price}


def _option(name: str, strike: float, expiry_ms: int, option_type: str) -> dict[str, Any]:
    return {
        "instrument_name": name,
        "strike": strike,
        "expiration_timestamp": expiry_ms,
        "option_type": option_type,
    }


def _ticker(name: str, *, mark_price: float, mark_iv: float, bid: float = 0.0, ask: float = 0.0) -> dict[str, Any]:
    return {
        "instrument_name": name,
        "mark_price": mark_price,
        "mark_iv": mark_iv,
        "best_bid_price": bid,
        "best_ask_price": ask,
    }


class TestOptionChain:
    def test_put_at_strike_returns_deepest_itm(self):
        p1 = OptionContract(
            instrument_name="p1",
            underlying="BTC",
            strike=50_000.0,
            expiry_ts_ms=0,
            is_put=True,
            mark_price=0.0,
            mark_iv=0.6,
            bid=0.0,
            ask=0.0,
        )
        p2 = OptionContract(
            instrument_name="p2",
            underlying="BTC",
            strike=45_000.0,
            expiry_ts_ms=0,
            is_put=True,
            mark_price=0.0,
            mark_iv=0.65,
            bid=0.0,
            ask=0.0,
        )
        chain = OptionChain(
            underlying="BTC",
            expiry_ts_ms=0,
            puts=(p1, p2),
            calls=(),
        )
        best = chain.put_at_strike(48_000.0)
        assert best is not None
        assert best.strike == 45_000.0

    def test_put_at_strike_returns_none_when_no_match(self):
        chain = OptionChain(
            underlying="BTC",
            expiry_ts_ms=0,
            puts=(
                OptionContract(
                    instrument_name="p1",
                    underlying="BTC",
                    strike=50_000.0,
                    expiry_ts_ms=0,
                    is_put=True,
                    mark_price=0.0,
                    mark_iv=0.6,
                    bid=0.0,
                    ask=0.0,
                ),
            ),
            calls=(),
        )
        assert chain.put_at_strike(45_000.0) is None


class TestDeribitClient:
    def _client(self, **kwargs) -> DeribitClient:
        mcp = FakeDeribitMcp(
            instruments=[
                _option("BTC-28JUN24-50000-P", 50_000.0, 1_719_619_200_000, "put"),
                _option("BTC-28JUN24-60000-P", 60_000.0, 1_719_619_200_000, "put"),
                _option("BTC-28JUN24-70000-C", 70_000.0, 1_719_619_200_000, "call"),
                _option("BTC-27DEC24-50000-P", 50_000.0, 1_735_257_600_000, "put"),
            ],
            tickers=[
                _ticker("BTC-28JUN24-50000-P", mark_price=0.02, mark_iv=60.0, bid=0.018, ask=0.022),
                _ticker("BTC-28JUN24-60000-P", mark_price=0.03, mark_iv=58.0, bid=0.028, ask=0.032),
                _ticker("BTC-28JUN24-70000-C", mark_price=0.015, mark_iv=55.0, bid=0.012, ask=0.018),
            ],
            index_price=60_500.0,
        )
        return DeribitClient(mcp_client=mcp, **kwargs)

    def test_connection_endpoint(self):
        client = self._client()
        assert "deribit.com" in client.connection_endpoint()

    def test_fetch_chain_partitions_puts_and_calls(self):
        client = self._client()
        chain = client.fetch_option_chain(underlying="BTC", expiry_ts_ms=1_719_619_200_000)
        assert len(chain.puts) == 2
        assert len(chain.calls) == 1
        # Puts sorted descending, calls ascending
        assert chain.puts[0].strike == 60_000.0
        assert chain.puts[1].strike == 50_000.0

    def test_fetch_chain_filters_by_expiry(self):
        client = self._client()
        chain = client.fetch_option_chain(underlying="BTC", expiry_ts_ms=1_719_619_200_000)
        expiries = {c.expiry_ts_ms for c in chain.puts}
        assert expiries == {1_719_619_200_000}

    def test_fetch_atm_iv_returns_decimal(self):
        client = self._client()
        iv = client.fetch_atm_iv(
            underlying="BTC",
            expiry_ts_ms=1_719_619_200_000,
            spot_override=60_500.0,
        )
        assert 0.4 < iv < 0.7  # 60% ATM-ish

    def test_fetch_atm_iv_empty_chain_returns_zero(self):
        mcp = FakeDeribitMcp(instruments=[], tickers=[], index_price=60_000.0)
        client = DeribitClient(mcp_client=mcp)
        iv = client.fetch_atm_iv(
            underlying="BTC",
            expiry_ts_ms=1_719_619_200_000,
            spot_override=60_000.0,
        )
        assert iv == 0.0

    def test_place_order_rejected_when_disabled(self):
        client = self._client()
        req = OrderRequest(
            symbol="BTC-28JUN24-60000-P",
            side=Side.BUY,
            qty=1.0,
        )
        result = asyncio.run(client.place_order(req))
        assert result.status == OrderStatus.REJECTED

    def test_place_order_raises_when_enabled_but_unimplemented(self):
        client = self._client(allow_orders=True)
        req = OrderRequest(
            symbol="BTC-28JUN24-60000-P",
            side=Side.BUY,
            qty=1.0,
        )
        with pytest.raises(NotImplementedError):
            asyncio.run(client.place_order(req))

    def test_cancel_get_balance_safe_defaults(self):
        client = self._client()
        assert asyncio.run(client.cancel_order("X", "Y")) is False
        assert asyncio.run(client.get_positions()) == []
        assert asyncio.run(client.get_balance()) == {}


class TestAtmIvToTailSigma:
    def test_pass_through_when_in_range(self):
        assert atm_iv_to_tail_sigma(0.65, 30) == pytest.approx(0.65)

    def test_clamps_negative(self):
        assert atm_iv_to_tail_sigma(-0.1, 30) == 0.0

    def test_clamps_above_max(self):
        assert atm_iv_to_tail_sigma(5.0, 30) == 3.0
