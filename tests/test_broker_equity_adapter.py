"""Tests for :mod:`apex_predator.core.broker_equity_adapter`.

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
"""
from __future__ import annotations

import asyncio

import pytest

from apex_predator.core.broker_equity_adapter import (
    BrokerEquityAdapter,
    NullBrokerEquityAdapter,
    make_poller_for,
)
from apex_predator.core.broker_equity_poller import BrokerEquityPoller
from apex_predator.core.broker_equity_reconciler import BrokerEquityReconciler

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
        from apex_predator.venues.ibkr import IbkrClientPortalVenue

        adapter = IbkrClientPortalVenue()
        assert isinstance(adapter, BrokerEquityAdapter)

    def test_tastytrade_venue_satisfies_protocol(self) -> None:
        """The production Tastytrade venue already fits structurally."""
        from apex_predator.venues.tastytrade import TastytradeVenue

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
            _FixedAdapter(), refresh_s=12.5, stale_after_s=99.0,
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
