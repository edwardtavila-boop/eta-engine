"""
EVOLUTIONARY TRADING ALGO  //  tests.test_venues
====================================
Order model validation, idempotency, signing, contract resolution,
circuit breaker, SmartRouter dispatch + failover.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eta_engine.venues import (
    BybitVenue,
    IbkrClientPortalVenue,
    OkxVenue,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    SmartRouter,
    TastytradeVenue,
    TradovateVenue,
)
from eta_engine.venues.router import CircuitBreaker


class TestOrderRequest:
    def test_valid_request(self) -> None:
        req = OrderRequest(symbol="ETH/USDT:USDT", side=Side.BUY, qty=0.5, order_type=OrderType.LIMIT, price=3500.0)
        assert req.qty == 0.5
        assert req.side is Side.BUY
        assert req.reduce_only is False

    def test_qty_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            OrderRequest(symbol="ETH/USDT:USDT", side=Side.BUY, qty=0.0)

    def test_default_market(self) -> None:
        req = OrderRequest(symbol="ETH/USDT:USDT", side=Side.SELL, qty=1.0)
        assert req.order_type is OrderType.MARKET


class TestIdempotency:
    def test_idempotency_deterministic(self) -> None:
        venue = BybitVenue()
        req = OrderRequest(symbol="ETH/USDT:USDT", side=Side.BUY, qty=0.5, price=3500.0)
        a = venue.idempotency_key(req)
        b = venue.idempotency_key(req)
        assert a == b
        assert len(a) == 32

    def test_idempotency_honors_client_id(self) -> None:
        venue = BybitVenue()
        req = OrderRequest(symbol="ETH/USDT:USDT", side=Side.BUY, qty=0.5, client_order_id="my-id-123")
        assert venue.idempotency_key(req) == "my-id-123"

    def test_idempotency_differs_by_venue(self) -> None:
        req = OrderRequest(symbol="ETH/USDT:USDT", side=Side.BUY, qty=0.5, price=3500.0)
        assert BybitVenue().idempotency_key(req) != OkxVenue().idempotency_key(req)


class TestBybitSigning:
    def test_bybit_signing_deterministic(self) -> None:
        venue = BybitVenue(api_key="KEY", api_secret="SECRET")
        sig_a = venue._sign("1700000000000", "5000", '{"symbol":"ETHUSDT"}')
        sig_b = venue._sign("1700000000000", "5000", '{"symbol":"ETHUSDT"}')
        assert sig_a == sig_b
        assert len(sig_a) == 64  # sha256 hex digest

    def test_bybit_signing_different_secret(self) -> None:
        a = BybitVenue(api_key="KEY", api_secret="SECRET_A")
        b = BybitVenue(api_key="KEY", api_secret="SECRET_B")
        sig_a = a._sign("1700000000000", "5000", "payload")
        sig_b = b._sign("1700000000000", "5000", "payload")
        assert sig_a != sig_b

    def test_bybit_signing_different_timestamp(self) -> None:
        venue = BybitVenue(api_key="KEY", api_secret="SECRET")
        assert venue._sign("1", "5000", "p") != venue._sign("2", "5000", "p")

    def test_bybit_symbol_mapping_ccxt_form(self) -> None:
        venue = BybitVenue()
        assert venue._native_symbol("ETH/USDT:USDT") == "ETHUSDT"
        assert venue._native_symbol("BTC/USDT:USDT") == "BTCUSDT"

    def test_bybit_symbol_mapping_passthrough(self) -> None:
        venue = BybitVenue()
        assert venue._native_symbol("ETHUSDT") == "ETHUSDT"
        assert venue._native_symbol("SOLUSDT") == "SOLUSDT"

    def test_bybit_testnet_host(self) -> None:
        assert BybitVenue(testnet=True)._host().endswith("api-testnet.bybit.com")
        assert BybitVenue(testnet=False)._host() == "https://api.bybit.com"


class TestTradovateContracts:
    def test_tradovate_contract_resolution_quarterly(self) -> None:
        venue = TradovateVenue()
        # Mid-January 2026 → should pick March (H) contract
        jan = datetime(2026, 1, 15, tzinfo=UTC)
        sym = venue.resolve_contract("MNQ", ref=jan)
        assert sym.startswith("MNQ")
        assert sym[3] in {"H", "M", "U", "Z"}
        assert sym[3] == "H"  # March
        assert sym[-1] == "6"  # 2026

    def test_tradovate_contract_rollover_threshold(self) -> None:
        venue = TradovateVenue()
        # 3rd Friday of March 2026 = March 20. 3 business days prior = March 17 (Tue).
        # Should have rolled to June (M).
        near_expiry = datetime(2026, 3, 17, tzinfo=UTC)
        sym = venue.resolve_contract("MNQ", ref=near_expiry)
        assert sym[3] == "M"  # June
        assert sym[-1] == "6"

    def test_tradovate_contract_resolution_mid_cycle(self) -> None:
        venue = TradovateVenue()
        # Mid-July → September (U) contract
        jul = datetime(2026, 7, 15, tzinfo=UTC)
        sym = venue.resolve_contract("NQ", ref=jul)
        assert sym == "NQU6"


class TestCircuitBreaker:
    def test_circuit_breaker_opens_after_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3, reset_timeout_s=60)
        assert cb.is_open() is False
        for _ in range(3):
            cb.record_failure()
        assert cb.is_open() is True

    def test_circuit_breaker_resets_after_timeout(self) -> None:
        clock = {"t": 1000.0}
        cb = CircuitBreaker(failure_threshold=2, reset_timeout_s=60, _time_fn=lambda: clock["t"])
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open() is True
        clock["t"] = 1061.0  # past cooldown
        assert cb.is_open() is False

    def test_circuit_breaker_success_resets(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()  # only 1 since reset
        assert cb.is_open() is False


class TestSmartRouter:
    def test_crypto_routes_to_bybit(self) -> None:
        router = SmartRouter()
        assert isinstance(router.choose_venue("ETH/USDT:USDT", 0.5), BybitVenue)
        assert isinstance(router.choose_venue("BTCUSDT", 1.0), BybitVenue)

    def test_crypto_routes_to_okx_when_pinned(self) -> None:
        router = SmartRouter(preferred_crypto_venue="okx")
        assert isinstance(router.choose_venue("ETH/USDT:USDT", 0.5), OkxVenue)
        assert isinstance(router.choose_venue("BTCUSDT", 1.0), OkxVenue)

    def test_futures_routes_to_ibkr_by_default(self) -> None:
        # Broker dormancy mandate 2026-04-24: IBKR is the active default
        # futures venue. Tradovate is DORMANT. See venues/router.py.
        router = SmartRouter()
        assert isinstance(router.choose_venue("MNQM5", 1), IbkrClientPortalVenue)
        assert isinstance(router.choose_venue("NQM6", 1), IbkrClientPortalVenue)

    def test_futures_routes_to_ibkr_when_pinned(self) -> None:
        router = SmartRouter(preferred_futures_venue="ibkr")
        assert isinstance(router.choose_venue("MNQM5", 1), IbkrClientPortalVenue)

    def test_futures_routes_to_tastytrade_when_pinned(self) -> None:
        router = SmartRouter(preferred_futures_venue="tastytrade")
        assert isinstance(router.choose_venue("MNQM5", 1), TastytradeVenue)

    @pytest.mark.asyncio
    async def test_place_with_failover_primary(self) -> None:
        router = SmartRouter()
        req = OrderRequest(symbol="ETH/USDT:USDT", side=Side.BUY, qty=0.5)
        result = await router.place_with_failover(req)
        assert result.order_id
        assert result.status is OrderStatus.OPEN

    @pytest.mark.asyncio
    async def test_smart_router_failover_on_primary_reject(self) -> None:
        class _RejectBybit(BybitVenue):
            async def place_order(self, request: OrderRequest) -> OrderResult:
                return OrderResult(order_id="rej-1", status=OrderStatus.REJECTED, raw={"retCode": 10001})

        class _OkOkx(OkxVenue):
            async def place_order(self, request: OrderRequest) -> OrderResult:
                return OrderResult(order_id="okx-1", status=OrderStatus.OPEN)

        router = SmartRouter(bybit=_RejectBybit(), okx=_OkOkx())
        req = OrderRequest(symbol="ETHUSDT", side=Side.BUY, qty=0.5)
        result = await router.place_with_failover(req)
        assert result.order_id == "okx-1"
        assert result.status is OrderStatus.OPEN
        assert any(e["reason"] == "rejected" for e in router._failover_log)
