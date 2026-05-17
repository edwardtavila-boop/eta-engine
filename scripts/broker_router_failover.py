from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from logging import Logger


class _OrderLike(Protocol):
    bot_id: str
    signal_id: str
    symbol: str


class _VenueLike(Protocol):
    name: str

    async def place_order(self, request: object) -> object: ...


class _CircuitLike(Protocol):
    def is_open(self) -> bool: ...

    def record_success(self) -> None: ...

    def record_failure(self) -> None: ...


class _RoutingConfigLike(Protocol):
    def failover_chain(self, bot_id: str, symbol: str) -> tuple[str, ...]: ...


class BrokerRouterFailover:
    """Own broker-router failover chain walking and circuit inspection."""

    def __init__(
        self,
        *,
        routing_config: _RoutingConfigLike,
        smart_router: object,
        resolve_venue_adapter: Callable[[str, _OrderLike], _VenueLike | None],
        is_transient_failure: Callable[[BaseException], bool],
        logger: Logger,
    ) -> None:
        self._routing_config = routing_config
        self._smart_router = smart_router
        self._resolve_venue_adapter = resolve_venue_adapter
        self._is_transient_failure = is_transient_failure
        self._logger = logger

    async def place_with_failover_chain(
        self,
        order: _OrderLike,
        primary: _VenueLike,
        request: object,
    ) -> tuple[object, _VenueLike]:
        chain = self._routing_config.failover_chain(order.bot_id, order.symbol)
        attempted: list[str] = []
        last_exc: BaseException | None = None
        venue = primary
        chain_idx = 0

        while venue is not None and chain_idx < len(chain):
            attempted.append(venue.name)
            circuit = self.venue_circuit(venue.name)
            if circuit is not None and circuit.is_open():
                self._logger.warning(
                    "broker_router failover hop: venue=%s circuit OPEN; trying next-in-chain bot=%s signal=%s",
                    venue.name,
                    order.bot_id,
                    order.signal_id,
                )
                chain_idx += 1
                venue = self.next_chain_venue(chain, chain_idx, order)
                continue

            try:
                result = await venue.place_order(request)
                if circuit is not None:
                    circuit.record_success()
                return result, venue
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if circuit is not None:
                    circuit.record_failure()
                if not self._is_transient_failure(exc):
                    raise
                self._logger.warning(
                    "broker_router failover hop: venue=%s transient failure (%s); "
                    "trying next-in-chain bot=%s signal=%s",
                    venue.name,
                    exc,
                    order.bot_id,
                    order.signal_id,
                )
                chain_idx += 1
                venue = self.next_chain_venue(chain, chain_idx, order)

        if last_exc is not None:
            raise last_exc
        msg = (
            "failover chain exhausted with no attempts "
            f"attempted={attempted!r} chain={chain!r}"
        )
        raise RuntimeError(msg)

    def next_chain_venue(
        self,
        chain: tuple[str, ...],
        idx: int,
        order: _OrderLike,
    ) -> _VenueLike | None:
        """Look up the venue adapter for ``chain[idx]``, or ``None``."""
        if idx >= len(chain):
            return None
        return self._resolve_venue_adapter(chain[idx], order)

    def venue_circuit(self, venue_name: str) -> _CircuitLike | None:
        """Return the per-venue CircuitBreaker on the SmartRouter, or None."""
        circuits = getattr(self._smart_router, "_venue_circuits", None)
        if isinstance(circuits, dict):
            return circuits.get(venue_name)
        return None

    def venue_circuit_states(self) -> dict[str, str]:
        """Snapshot every venue circuit as ``{name: closed|open|half-open}``."""
        circuits = getattr(self._smart_router, "_venue_circuits", None)
        if not isinstance(circuits, dict):
            return {}
        out: dict[str, str] = {}
        for name, breaker in circuits.items():
            try:
                if breaker.is_open():
                    out[name] = "open"
                    continue
                failures = int(getattr(breaker, "_failures", 0) or 0)
                if failures > 0:
                    out[name] = "half-open"
                else:
                    out[name] = "closed"
            except Exception:  # noqa: BLE001
                out[name] = "unknown"
        return out
