from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from eta_engine.scripts.broker_router_failover import BrokerRouterFailover
from eta_engine.venues.base import OrderRequest, OrderResult, OrderStatus, OrderType, Side


class _Circuit:
    def __init__(self, *, open_state: bool = False, failures: int = 0) -> None:
        self._open_state = open_state
        self._failures = failures
        self.successes = 0
        self.failures_recorded = 0

    def is_open(self) -> bool:
        return self._open_state

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self) -> None:
        self.failures_recorded += 1


class _UnknownCircuit:
    def is_open(self) -> bool:
        raise RuntimeError("broken breaker")


class _Venue:
    def __init__(self, name: str, results: list[object] | None = None) -> None:
        self.name = name
        self._results = list(results or [])
        self.calls: list[OrderRequest] = []

    async def place_order(self, request: OrderRequest) -> OrderResult:
        self.calls.append(request)
        if self._results:
            nxt = self._results.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return OrderResult(order_id=f"{self.name}-oid", status=OrderStatus.FILLED, filled_qty=request.qty)


def _request() -> OrderRequest:
    return OrderRequest(
        symbol="MNQ",
        side=Side.BUY,
        qty=1.0,
        order_type=OrderType.LIMIT,
        price=25_000.0,
        client_order_id="sig-failover",
    )


def _order() -> SimpleNamespace:
    return SimpleNamespace(bot_id="alpha", signal_id="sig-failover", symbol="MNQ")


def test_place_with_failover_chain_skips_open_circuit_and_uses_next_venue() -> None:
    primary = _Venue("primary")
    backup = _Venue(
        "backup",
        [OrderResult(order_id="backup-1", status=OrderStatus.FILLED, filled_qty=1.0)],
    )
    circuits = {"primary": _Circuit(open_state=True), "backup": _Circuit()}
    venues = {"primary": primary, "backup": backup}
    helper = BrokerRouterFailover(
        routing_config=SimpleNamespace(failover_chain=lambda bot_id, symbol: ("primary", "backup")),
        smart_router=SimpleNamespace(_venue_circuits=circuits),
        resolve_venue_adapter=lambda venue_name, order: venues.get(venue_name),
        is_transient_failure=lambda exc: isinstance(exc, TimeoutError),
        logger=logging.getLogger("test_broker_router_failover"),
    )

    result, venue = asyncio.run(helper.place_with_failover_chain(_order(), primary, _request()))

    assert venue is backup
    assert result.order_id == "backup-1"
    assert len(primary.calls) == 0
    assert len(backup.calls) == 1


def test_place_with_failover_chain_aborts_on_deterministic_failure() -> None:
    primary = _Venue("primary", [ValueError("bad request")])
    backup = _Venue("backup")
    helper = BrokerRouterFailover(
        routing_config=SimpleNamespace(failover_chain=lambda bot_id, symbol: ("primary", "backup")),
        smart_router=SimpleNamespace(_venue_circuits={"primary": _Circuit(), "backup": _Circuit()}),
        resolve_venue_adapter=lambda venue_name, order: {"primary": primary, "backup": backup}.get(venue_name),
        is_transient_failure=lambda exc: isinstance(exc, TimeoutError),
        logger=logging.getLogger("test_broker_router_failover"),
    )

    try:
        asyncio.run(helper.place_with_failover_chain(_order(), primary, _request()))
    except ValueError as exc:
        assert "bad request" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected deterministic failure to abort failover")

    assert len(primary.calls) == 1
    assert len(backup.calls) == 0


def test_venue_circuit_states_reports_open_half_open_closed_and_unknown() -> None:
    helper = BrokerRouterFailover(
        routing_config=SimpleNamespace(failover_chain=lambda bot_id, symbol: ("primary",)),
        smart_router=SimpleNamespace(
            _venue_circuits={
                "openish": _Circuit(open_state=True),
                "half": _Circuit(failures=2),
                "closed": _Circuit(),
                "unknown": _UnknownCircuit(),
            }
        ),
        resolve_venue_adapter=lambda venue_name, order: None,
        is_transient_failure=lambda exc: False,
        logger=logging.getLogger("test_broker_router_failover"),
    )

    assert helper.venue_circuit_states() == {
        "openish": "open",
        "half": "half-open",
        "closed": "closed",
        "unknown": "unknown",
    }
