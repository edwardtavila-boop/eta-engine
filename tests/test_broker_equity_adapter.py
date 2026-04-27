"""Tests for :mod:`eta_engine.core.broker_equity_adapter`.

R1 contract layer -- pins the structural protocol that any broker
venue adapter must satisfy to drive :class:`BrokerEquityReconciler` /
:class:`BrokerEquityPoller`.

Sections
--------
TestProtocolStructuralFit
  Verifies ``isinstance`` returns the right answer for adapters that
  do / do not match the structural shape, including the two
  production venues (IBKR, Tastytrade) and the bundled
  :class:`NullBrokerEquityAdapter`.

TestNullBrokerEquityAdapter
  Pins the behaviour of the no-op adapter -- always returns ``None``,
  exposes a ``name`` attr, and fits the protocol.

TestMakePollerFor
  Pins :func:`make_poller_for`: returns an unstarted
  :class:`BrokerEquityPoller`, forwards parameters, raises
  :class:`TypeError` for non-conforming inputs.

TestEndToEndAdapterPollerReconciler
  Smoke test wiring an adapter through the factory through the
  reconciler so any future regression in the contract is caught.

TestRouterBackedBrokerEquityAdapter
  Pins the v0.1.64 router-aware adapter that proxies to whichever
  futures venue the router currently prefers (IBKR primary, Tastytrade
  fallback under the broker dormancy mandate). Verifies failover
  semantics, exception swallowing, and protocol fit.
"""

from __future__ import annotations

import asyncio

import pytest

from eta_engine.core.broker_equity_adapter import (
    BrokerEquityAdapter,
    BrokerEquityNotAvailableError,
    NullBrokerEquityAdapter,
    RouterBackedBrokerEquityAdapter,
    SafeBrokerEquityAdapter,
    make_poller_for,
)
from eta_engine.core.broker_equity_poller import BrokerEquityPoller
from eta_engine.core.broker_equity_reconciler import BrokerEquityReconciler

# ---------------------------------------------------------------------------
# Helper fakes
# ---------------------------------------------------------------------------


class _FixedAdapter:
    """Minimal structural fit -- for protocol-fit / factory tests."""

    def __init__(self, name: str = "fixed", value: float | None = 50_000.0) -> None:
        self.name = name
        self._value = value

    async def get_net_liquidation(self) -> float | None:
        return self._value


class _MissingNameAdapter:
    async def get_net_liquidation(self) -> float | None:
        return 1.0


class _MissingMethodAdapter:
    name = "no_method"


class _NotAdapterAtAll:
    """Random object -- definitely not a broker."""

    flavour = "vanilla"


# ---------------------------------------------------------------------------
# Protocol structural fit
# ---------------------------------------------------------------------------


class TestProtocolStructuralFit:
    """``isinstance(x, BrokerEquityAdapter)`` should match the spec."""

    def test_fixed_adapter_satisfies_protocol(self) -> None:
        adapter = _FixedAdapter()
        assert isinstance(adapter, BrokerEquityAdapter)

    def test_null_adapter_satisfies_protocol(self) -> None:
        adapter = NullBrokerEquityAdapter()
        assert isinstance(adapter, BrokerEquityAdapter)

    def test_missing_name_attr_fails_protocol_check(self) -> None:
        adapter = _MissingNameAdapter()
        assert not isinstance(adapter, BrokerEquityAdapter)

    def test_missing_method_attr_fails_protocol_check(self) -> None:
        adapter = _MissingMethodAdapter()
        assert not isinstance(adapter, BrokerEquityAdapter)

    def test_random_object_fails_protocol_check(self) -> None:
        assert not isinstance(_NotAdapterAtAll(), BrokerEquityAdapter)

    def test_ibkr_adapter_satisfies_protocol(self) -> None:
        """The production IBKR adapter already fits structurally."""
        # Lazy import keeps test import cheap and avoids triggering
        # IBKR optional deps when this test file is collected on a
        # machine without httpx / pydantic-extras.
        from eta_engine.venues.ibkr import IbkrClientPortalVenue

        adapter = IbkrClientPortalVenue()
        assert isinstance(adapter, BrokerEquityAdapter)

    def test_tastytrade_venue_satisfies_protocol(self) -> None:
        """The production Tastytrade venue already fits structurally."""
        from eta_engine.venues.tastytrade import TastytradeVenue

        venue = TastytradeVenue()
        assert isinstance(venue, BrokerEquityAdapter)


# ---------------------------------------------------------------------------
# Null adapter behaviour
# ---------------------------------------------------------------------------


class TestNullBrokerEquityAdapter:
    """The bundled no-op adapter."""

    def test_default_name_is_null(self) -> None:
        assert NullBrokerEquityAdapter().name == "null"

    def test_custom_name_is_preserved(self) -> None:
        assert NullBrokerEquityAdapter(name="paper").name == "paper"

    @pytest.mark.asyncio
    async def test_get_net_liquidation_returns_none(self) -> None:
        adapter = NullBrokerEquityAdapter()
        assert await adapter.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_get_net_liquidation_is_repeatable(self) -> None:
        adapter = NullBrokerEquityAdapter()
        for _ in range(5):
            assert await adapter.get_net_liquidation() is None


# ---------------------------------------------------------------------------
# make_poller_for factory
# ---------------------------------------------------------------------------


class TestMakePollerFor:
    """Build :class:`BrokerEquityPoller` from a protocol-conformant adapter."""

    def test_returns_broker_equity_poller_for_valid_adapter(self) -> None:
        poller = make_poller_for(_FixedAdapter())
        assert isinstance(poller, BrokerEquityPoller)

    def test_returned_poller_is_not_yet_started(self) -> None:
        poller = make_poller_for(_FixedAdapter())
        assert poller.is_running() is False

    def test_forwards_adapter_name_to_poller(self) -> None:
        poller = make_poller_for(_FixedAdapter(name="mycustom"))
        assert poller.name == "mycustom"

    def test_forwards_refresh_and_stale_parameters(self) -> None:
        poller = make_poller_for(
            _FixedAdapter(),
            refresh_s=12.5,
            stale_after_s=99.0,
        )
        # poller stores them privately -- verify via the dataclass-ish attrs
        assert poller._refresh_s == 12.5  # noqa: SLF001
        assert poller._stale_after_s == 99.0  # noqa: SLF001

    def test_uses_default_refresh_and_stale_when_omitted(self) -> None:
        poller = make_poller_for(_FixedAdapter())
        assert poller._refresh_s == 5.0  # noqa: SLF001
        assert poller._stale_after_s == 30.0  # noqa: SLF001

    def test_raises_typeerror_for_missing_name(self) -> None:
        with pytest.raises(TypeError, match="BrokerEquityAdapter"):
            make_poller_for(_MissingNameAdapter())  # type: ignore[arg-type]

    def test_raises_typeerror_for_missing_method(self) -> None:
        with pytest.raises(TypeError, match="BrokerEquityAdapter"):
            make_poller_for(_MissingMethodAdapter())  # type: ignore[arg-type]

    def test_typeerror_message_names_offending_class(self) -> None:
        with pytest.raises(TypeError) as exc:
            make_poller_for(_NotAdapterAtAll())  # type: ignore[arg-type]
        assert "_NotAdapterAtAll" in str(exc.value)

    def test_null_adapter_round_trips_through_factory(self) -> None:
        poller = make_poller_for(NullBrokerEquityAdapter(name="paper"))
        assert poller.name == "paper"


# ---------------------------------------------------------------------------
# End-to-end: adapter -> poller -> reconciler
# ---------------------------------------------------------------------------


class TestEndToEndAdapterPollerReconciler:
    """Smoke-test the contract from adapter through to a reconcile tick."""

    @pytest.mark.asyncio
    async def test_fixed_adapter_drives_reconciler_within_tolerance(self) -> None:
        adapter = _FixedAdapter(name="smoke", value=50_000.0)
        poller = make_poller_for(adapter, refresh_s=0.05, stale_after_s=5.0)
        await poller.start()
        try:
            assert poller.current() == 50_000.0
            rec = BrokerEquityReconciler(broker_equity_source=poller.current)
            result = rec.reconcile(logical_equity_usd=50_010.0)
            assert result.in_tolerance is True
            assert result.broker_equity_usd == 50_000.0
            assert result.drift_usd == pytest.approx(10.0)
        finally:
            await poller.stop()

    @pytest.mark.asyncio
    async def test_null_adapter_drives_reconciler_no_broker_data(self) -> None:
        adapter = NullBrokerEquityAdapter(name="paper")
        poller = make_poller_for(adapter, refresh_s=0.05, stale_after_s=5.0)
        await poller.start()
        try:
            # null adapter never produces a value -> poller.current() is None
            assert poller.current() is None
            rec = BrokerEquityReconciler(broker_equity_source=poller.current)
            result = rec.reconcile(logical_equity_usd=50_000.0)
            assert result.reason == "no_broker_data"
            assert result.in_tolerance is True
            assert result.broker_equity_usd is None
        finally:
            await poller.stop()

    @pytest.mark.asyncio
    async def test_drift_above_tolerance_flips_reconciler(self) -> None:
        # broker $200 below logical -- way past the default $50 tolerance
        adapter = _FixedAdapter(name="drift", value=49_800.0)
        poller = make_poller_for(adapter, refresh_s=0.05, stale_after_s=5.0)
        await poller.start()
        try:
            rec = BrokerEquityReconciler(
                broker_equity_source=poller.current,
                tolerance_usd=50.0,
                tolerance_pct=0.001,
            )
            result = rec.reconcile(logical_equity_usd=50_000.0)
            assert result.in_tolerance is False
            assert result.reason == "broker_below_logical"
            assert result.drift_usd == pytest.approx(200.0)
        finally:
            await poller.stop()

    @pytest.mark.asyncio
    async def test_polling_loop_repeatedly_calls_adapter(self) -> None:
        """Sanity check: the poller actually exercises the adapter."""
        call_log: list[int] = []

        class _CountingAdapter:
            name = "counting"

            async def get_net_liquidation(self) -> float | None:
                call_log.append(1)
                return 50_000.0

        # Verify it fits the protocol before we trust it in the poller.
        counting: BrokerEquityAdapter = _CountingAdapter()
        assert isinstance(counting, BrokerEquityAdapter)

        poller = make_poller_for(counting, refresh_s=0.02, stale_after_s=5.0)
        await poller.start()
        try:
            await asyncio.sleep(0.12)
            # at minimum: 1 eager fetch + a few loop iterations
            assert len(call_log) >= 3
        finally:
            await poller.stop()


# ---------------------------------------------------------------------------
# Router-backed adapter (v0.1.64)
# ---------------------------------------------------------------------------


class _FakeFuturesVenue:
    """Stand-in for IBKR / Tastytrade with a controllable equity reading."""

    def __init__(self, name: str, equity: float | None = 50_000.0) -> None:
        self.name = name
        self._equity = equity
        self._raise_on_call: Exception | None = None
        self.calls: int = 0

    def set_equity(self, value: float | None) -> None:
        self._equity = value

    def set_raises(self, exc: Exception | None) -> None:
        self._raise_on_call = exc

    async def get_net_liquidation(self) -> float | None:
        self.calls += 1
        if self._raise_on_call is not None:
            raise self._raise_on_call
        return self._equity


class _NoEquityVenue:
    """Stand-in for a venue that lacks ``get_net_liquidation``."""

    def __init__(self, name: str = "no_equity") -> None:
        self.name = name


def _make_router(
    *,
    ibkr: object | None = None,
    tastytrade: object | None = None,
    preferred: str = "ibkr",
):
    """Construct a SmartRouter wired with fake venues, lazy-imported."""
    from eta_engine.venues.router import SmartRouter

    return SmartRouter(
        ibkr=ibkr or _FakeFuturesVenue("ibkr", equity=50_000.0),
        tastytrade=tastytrade or _FakeFuturesVenue("tastytrade", equity=49_900.0),
        preferred_futures_venue=preferred,
    )


class TestRouterBackedBrokerEquityAdapter:
    """v0.1.64 -- router-aware proxy that follows futures-broker failover."""

    def test_adapter_satisfies_protocol(self) -> None:
        adapter = RouterBackedBrokerEquityAdapter(_make_router())
        assert isinstance(adapter, BrokerEquityAdapter)

    def test_default_name_is_router_active_futures(self) -> None:
        adapter = RouterBackedBrokerEquityAdapter(_make_router())
        assert adapter.name == "router-active-futures"

    def test_custom_name_is_preserved(self) -> None:
        adapter = RouterBackedBrokerEquityAdapter(_make_router(), name="custom")
        assert adapter.name == "custom"

    def test_construct_with_non_router_raises_typeerror(self) -> None:
        with pytest.raises(TypeError, match="SmartRouter"):
            RouterBackedBrokerEquityAdapter("not a router")  # type: ignore[arg-type]

    def test_active_venue_name_reports_ibkr_when_router_prefers_ibkr(self) -> None:
        adapter = RouterBackedBrokerEquityAdapter(_make_router(preferred="ibkr"))
        assert adapter.active_venue_name == "ibkr"

    def test_active_venue_name_reports_tastytrade_on_failover(self) -> None:
        adapter = RouterBackedBrokerEquityAdapter(_make_router(preferred="tastytrade"))
        assert adapter.active_venue_name == "tastytrade"

    @pytest.mark.asyncio
    async def test_get_net_liquidation_reads_from_active_venue(self) -> None:
        ibkr = _FakeFuturesVenue("ibkr", equity=51_234.5)
        adapter = RouterBackedBrokerEquityAdapter(_make_router(ibkr=ibkr))
        assert await adapter.get_net_liquidation() == 51_234.5
        assert ibkr.calls == 1

    @pytest.mark.asyncio
    async def test_failover_routes_next_read_to_tastytrade(self) -> None:
        ibkr = _FakeFuturesVenue("ibkr", equity=50_000.0)
        tasty = _FakeFuturesVenue("tastytrade", equity=49_900.0)
        adapter = RouterBackedBrokerEquityAdapter(
            _make_router(ibkr=ibkr, tastytrade=tasty, preferred="ibkr"),
        )
        # First read -- IBKR primary.
        assert await adapter.get_net_liquidation() == 50_000.0
        assert ibkr.calls == 1
        assert tasty.calls == 0
        # Operator / circuit failover swaps the router preference.
        adapter._router._preferred_futures_venue = "tastytrade"  # noqa: SLF001
        assert await adapter.get_net_liquidation() == 49_900.0
        assert tasty.calls == 1

    @pytest.mark.asyncio
    async def test_returns_none_when_venue_lacks_reader(self) -> None:
        no_equity = _NoEquityVenue("ibkr")
        adapter = RouterBackedBrokerEquityAdapter(_make_router(ibkr=no_equity))
        assert await adapter.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_returns_none_when_reader_raises(self) -> None:
        ibkr = _FakeFuturesVenue("ibkr")
        ibkr.set_raises(RuntimeError("auth flap"))
        adapter = RouterBackedBrokerEquityAdapter(_make_router(ibkr=ibkr))
        assert await adapter.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_returns_none_when_choose_venue_raises(self) -> None:
        adapter = RouterBackedBrokerEquityAdapter(_make_router())

        def _boom(*_a: object, **_kw: object) -> object:
            raise RuntimeError("router internal")

        adapter._router.choose_venue = _boom  # type: ignore[method-assign]  # noqa: SLF001
        assert await adapter.get_net_liquidation() is None
        # active_venue_name should also degrade gracefully.
        assert adapter.active_venue_name is None

    @pytest.mark.asyncio
    async def test_returns_none_when_venue_returns_none(self) -> None:
        ibkr = _FakeFuturesVenue("ibkr", equity=None)
        adapter = RouterBackedBrokerEquityAdapter(_make_router(ibkr=ibkr))
        assert await adapter.get_net_liquidation() is None

    def test_tradovate_dormancy_substitution_is_respected(self) -> None:
        """Operator mandate 2026-04-24: requesting tradovate -> IBKR substitution."""
        ibkr = _FakeFuturesVenue("ibkr", equity=50_000.0)
        adapter = RouterBackedBrokerEquityAdapter(
            _make_router(ibkr=ibkr, preferred="tradovate"),
        )
        # The router substitutes "tradovate" -> "ibkr" at construction.
        # The adapter should follow.
        assert adapter.active_venue_name == "ibkr"

    @pytest.mark.asyncio
    async def test_adapter_drives_reconciler_within_tolerance(self) -> None:
        """End-to-end: router-backed adapter -> poller -> reconciler."""
        ibkr = _FakeFuturesVenue("ibkr", equity=50_000.0)
        adapter = RouterBackedBrokerEquityAdapter(_make_router(ibkr=ibkr))
        poller = make_poller_for(adapter, refresh_s=0.05, stale_after_s=5.0)
        await poller.start()
        try:
            assert poller.current() == 50_000.0
            rec = BrokerEquityReconciler(broker_equity_source=poller.current)
            result = rec.reconcile(logical_equity_usd=50_010.0)
            assert result.in_tolerance is True
            assert result.broker_equity_usd == 50_000.0
        finally:
            await poller.stop()

    @pytest.mark.asyncio
    async def test_failover_mid_polling_swaps_source(self) -> None:
        """Long-running poller picks up a router preference flip."""
        ibkr = _FakeFuturesVenue("ibkr", equity=50_000.0)
        tasty = _FakeFuturesVenue("tastytrade", equity=49_500.0)
        router = _make_router(ibkr=ibkr, tastytrade=tasty, preferred="ibkr")
        adapter = RouterBackedBrokerEquityAdapter(router)
        poller = make_poller_for(adapter, refresh_s=0.02, stale_after_s=5.0)
        await poller.start()
        try:
            await asyncio.sleep(0.06)
            assert poller.current() == 50_000.0
            # Mid-flight failover.
            router._preferred_futures_venue = "tastytrade"  # noqa: SLF001
            await asyncio.sleep(0.08)
            assert poller.current() == 49_500.0
        finally:
            await poller.stop()


# ---------------------------------------------------------------------------
# Safe-wrapping adapter (v0.1.65 H7)
# ---------------------------------------------------------------------------


class _RaisingAdapter:
    """Adapter that raises on every call -- for H7 wrapper testing."""

    name = "raising"

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or RuntimeError("simulated failure")

    async def get_net_liquidation(self) -> float | None:
        raise self._exc


class TestSafeBrokerEquityAdapter:
    """v0.1.65 H7 -- runtime-enforced 'MUST NOT raise' wrapper."""

    def test_satisfies_protocol(self) -> None:
        wrapped = SafeBrokerEquityAdapter(_FixedAdapter())
        assert isinstance(wrapped, BrokerEquityAdapter)

    def test_default_name_wraps_inner(self) -> None:
        wrapped = SafeBrokerEquityAdapter(_FixedAdapter(name="ibkr"))
        assert wrapped.name == "safe(ibkr)"

    def test_custom_name_override(self) -> None:
        wrapped = SafeBrokerEquityAdapter(
            _FixedAdapter(name="ibkr"),
            name="my-custom",
        )
        assert wrapped.name == "my-custom"

    def test_construct_with_non_protocol_raises(self) -> None:
        with pytest.raises(TypeError, match="BrokerEquityAdapter"):
            SafeBrokerEquityAdapter("not an adapter")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_forwards_happy_path(self) -> None:
        wrapped = SafeBrokerEquityAdapter(_FixedAdapter(value=12_345.6))
        assert await wrapped.get_net_liquidation() == 12_345.6

    @pytest.mark.asyncio
    async def test_forwards_none(self) -> None:
        wrapped = SafeBrokerEquityAdapter(_FixedAdapter(value=None))
        assert await wrapped.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_swallows_runtime_error(self) -> None:
        wrapped = SafeBrokerEquityAdapter(_RaisingAdapter(RuntimeError("auth")))
        assert await wrapped.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_swallows_value_error(self) -> None:
        wrapped = SafeBrokerEquityAdapter(_RaisingAdapter(ValueError("parse")))
        assert await wrapped.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_swallows_arbitrary_exception_subclass(self) -> None:
        class _CustomError(Exception):
            pass

        wrapped = SafeBrokerEquityAdapter(
            _RaisingAdapter(_CustomError("anything")),
        )
        assert await wrapped.get_net_liquidation() is None

    @pytest.mark.asyncio
    async def test_wraps_router_backed_adapter(self) -> None:
        """Composition smoke test: Safe(RouterBacked(router)) works."""
        ibkr = _FakeFuturesVenue("ibkr", equity=42_000.0)
        from eta_engine.venues.router import SmartRouter

        router = SmartRouter(ibkr=ibkr, preferred_futures_venue="ibkr")
        inner = RouterBackedBrokerEquityAdapter(router)
        wrapped = SafeBrokerEquityAdapter(inner)
        assert isinstance(wrapped, BrokerEquityAdapter)
        assert await wrapped.get_net_liquidation() == 42_000.0


class TestBrokerEquityNotAvailableError:
    """v0.1.65 H6 -- exception class is importable + RuntimeError-derived."""

    def test_is_runtime_error_subclass(self) -> None:
        # Callers may catch RuntimeError; this contract must be stable.
        assert issubclass(BrokerEquityNotAvailableError, RuntimeError)

    def test_can_be_raised_and_caught_by_message(self) -> None:
        with pytest.raises(BrokerEquityNotAvailableError, match="no creds"):
            raise BrokerEquityNotAvailableError("no creds in live mode")
