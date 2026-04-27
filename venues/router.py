"""
EVOLUTIONARY TRADING ALGO  //  venues.router
================================
Smart routing with failover + circuit breakers.

Crypto (ETH/BTC/SOL/XRP USDT) -> Bybit primary, OKX fallback.

Futures (MNQ/NQ/ES/MES/RTY) -> IBKR primary, Tastytrade fallback.

Broker dormancy policy (operator mandate 2026-04-24)
----------------------------------------------------
Tradovate is funding-blocked and DORMANT until further notice. The
active live-futures broker set is IBKR + Tastytrade. A caller that
explicitly passes ``preferred_futures_venue="tradovate"`` will have
the request transparently substituted with :data:`DEFAULT_FUTURES_VENUE`
and a warning logged on the ``venues.router`` logger. When Tradovate
comes back online, flip :data:`DORMANT_BROKERS` back to the empty set.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from eta_engine.venues.base import OrderRequest, OrderResult, OrderStatus, VenueBase
from eta_engine.venues.bybit import BybitVenue
from eta_engine.venues.ibkr import IbkrClientPortalVenue
from eta_engine.venues.okx import OkxVenue
from eta_engine.venues.tastytrade import TastytradeVenue
from eta_engine.venues.tradovate import TradovateVenue

logger = logging.getLogger(__name__)

Urgency = Literal["low", "normal", "high"]

_CRYPTO_NATIVES = {"ETHUSDT", "BTCUSDT", "SOLUSDT", "XRPUSDT"}
_FUTURES_ROOTS = ("MNQ", "NQ", "ES", "MES", "RTY")

# ---------------------------------------------------------------------------
# Broker dormancy policy (operator mandate 2026-04-24)
# ---------------------------------------------------------------------------
#: Brokers that are funding-blocked or otherwise offline. Callers that
#: explicitly select a dormant broker get transparently routed to the
#: default active venue with a loud warning. Flip to ``frozenset()`` to
#: re-enable the full broker set.
DORMANT_BROKERS: frozenset[str] = frozenset({"tradovate"})

#: Preferred futures venue when the caller does not pin one. IBKR is
#: the most robust adapter in the active set.
DEFAULT_FUTURES_VENUE: str = "ibkr"

#: Preferred crypto venue when the caller does not pin one.
DEFAULT_CRYPTO_VENUE: str = "bybit"

#: Allowed values for ``preferred_futures_venue`` (including dormant
#: entries, which will be substituted at construction time).
_KNOWN_FUTURES_VENUES: frozenset[str] = frozenset({"tradovate", "ibkr", "tastytrade"})

#: Futures venues the router is allowed to route NEW orders to.
ACTIVE_FUTURES_VENUES: tuple[str, ...] = tuple(
    v for v in ("ibkr", "tastytrade", "tradovate") if v not in DORMANT_BROKERS
)


def _resolve_preferred_futures_venue(requested: str) -> str:
    """Substitute a dormant broker request with the active default.

    Normalizes case, validates the value is a known futures venue, and
    rewrites any dormant request to :data:`DEFAULT_FUTURES_VENUE` after
    logging a warning. The original value is preserved in the log so
    operators can audit intent vs. effective routing.
    """
    norm = requested.strip().lower()
    if norm not in _KNOWN_FUTURES_VENUES:
        msg = f"preferred_futures_venue must be one of {sorted(_KNOWN_FUTURES_VENUES)}, got {requested!r}"
        raise ValueError(msg)
    if norm in DORMANT_BROKERS:
        logger.warning(
            "broker_dormancy: requested preferred_futures_venue=%r is DORMANT; "
            "substituting %r (operator mandate 2026-04-24)",
            norm,
            DEFAULT_FUTURES_VENUE,
        )
        return DEFAULT_FUTURES_VENUE
    return norm


def _is_futures(symbol: str) -> bool:
    return symbol.upper().startswith(_FUTURES_ROOTS)


def _is_crypto(symbol: str) -> bool:
    up = symbol.upper()
    if up in _CRYPTO_NATIVES:
        return True
    return "/" in up or "USDT" in up or "USDC" in up


@dataclass
class CircuitBreaker:
    """Trip after N failures inside the reset window; resets after cooldown."""

    failure_threshold: int = 5
    reset_timeout_s: int = 60
    _failures: int = 0
    _opened_at: float | None = field(default=None, repr=False)
    _time_fn: callable[[], float] = field(default=time.monotonic, repr=False)

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = self._time_fn()

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if self._time_fn() - self._opened_at >= self.reset_timeout_s:
            # cool-down elapsed → half-open: clear and give the venue another shot
            self._failures = 0
            self._opened_at = None
            return False
        return True


class SmartRouter:
    """Route orders to the right venue with automatic failover."""

    def __init__(
        self,
        bybit: BybitVenue | None = None,
        okx: OkxVenue | None = None,
        tradovate: TradovateVenue | None = None,
        ibkr: VenueBase | None = None,
        tastytrade: VenueBase | None = None,
        preferred_crypto_venue: str = DEFAULT_CRYPTO_VENUE,
        preferred_futures_venue: str = DEFAULT_FUTURES_VENUE,
    ) -> None:
        self.bybit = bybit or BybitVenue()
        self.okx = okx or OkxVenue()
        self.tradovate = tradovate or TradovateVenue()
        self.ibkr = ibkr or IbkrClientPortalVenue()
        self.tastytrade = tastytrade or TastytradeVenue()
        preferred_crypto_venue_norm = preferred_crypto_venue.strip().lower()
        if preferred_crypto_venue_norm not in {"bybit", "okx"}:
            raise ValueError("preferred_crypto_venue must be 'bybit' or 'okx'")
        self._preferred_crypto_venue = preferred_crypto_venue_norm
        # Dormant brokers (e.g. Tradovate while funding-blocked) are
        # transparently substituted with DEFAULT_FUTURES_VENUE. See the
        # module docstring for the full dormancy policy.
        self._preferred_futures_venue = _resolve_preferred_futures_venue(
            preferred_futures_venue,
        )
        self._failover_log: list[dict[str, object]] = []
        self._venue_circuits: dict[str, CircuitBreaker] = {
            self.bybit.name: CircuitBreaker(),
            self.okx.name: CircuitBreaker(),
            self.tradovate.name: CircuitBreaker(),
            self.ibkr.name: CircuitBreaker(),
            self.tastytrade.name: CircuitBreaker(),
        }

    def _venue_by_name(self, name: str) -> VenueBase | None:
        lookup = {
            self.bybit.name: self.bybit,
            self.okx.name: self.okx,
            self.tradovate.name: self.tradovate,
            self.ibkr.name: self.ibkr,
            self.tastytrade.name: self.tastytrade,
        }
        return lookup.get(name)

    # ----- routing -----

    def choose_venue(
        self,
        symbol: str,
        quantity: float = 0.0,
        urgency: Urgency = "normal",
    ) -> VenueBase:
        """Pick the primary venue for an order."""
        _ = quantity, urgency  # reserved for future size/urgency tuning
        if _is_futures(symbol):
            if self._preferred_futures_venue == "ibkr":
                return self.ibkr
            if self._preferred_futures_venue == "tastytrade":
                return self.tastytrade
            return self.tradovate
        if _is_crypto(symbol):
            return self.okx if self._preferred_crypto_venue == "okx" else self.bybit
        return self.bybit

    def _fallback_for(self, primary: VenueBase) -> VenueBase | None:
        if primary is self.bybit:
            return self.okx
        if primary is self.tradovate:
            return self.ibkr
        if primary is self.okx:
            return self.bybit
        if primary is self.ibkr:
            return self.tastytrade
        if primary is self.tastytrade:
            return self.ibkr
        return None

    # ----- execution -----

    async def place_with_failover(
        self,
        req: OrderRequest,
        max_attempts: int = 2,
        urgency: Urgency = "normal",
    ) -> OrderResult:
        """Try primary; on REJECTED or exception, try fallback (up to max_attempts)."""
        primary = self.choose_venue(req.symbol, req.qty, urgency)
        attempted: list[VenueBase] = []
        last_exc: Exception | None = None
        last_result: OrderResult | None = None
        last_venue_name: str | None = None

        venue: VenueBase | None = primary
        while venue is not None and len(attempted) < max_attempts:
            circuit = self._venue_circuits.get(venue.name)
            if circuit is not None and circuit.is_open():
                logger.warning("circuit OPEN for venue=%s, skipping", venue.name)
                self._failover_log.append({"venue": venue.name, "reason": "circuit_open", "ts": time.time()})
                attempted.append(venue)
                venue = self._fallback_for(venue)
                continue
            try:
                result = await venue.place_order(req)
                if result.status is OrderStatus.REJECTED:
                    if circuit is not None:
                        circuit.record_failure()
                    self._failover_log.append(
                        {"venue": venue.name, "reason": "rejected", "ts": time.time(), "order_id": result.order_id}
                    )
                    result = result.model_copy(update={"raw": {**result.raw, "venue": venue.name}})
                    last_result = result
                    last_venue_name = venue.name
                    attempted.append(venue)
                    venue = self._fallback_for(venue)
                    continue
                if circuit is not None:
                    circuit.record_success()
                return result.model_copy(update={"raw": {**result.raw, "venue": venue.name}})
            except Exception as exc:  # noqa: BLE001
                if circuit is not None:
                    circuit.record_failure()
                last_exc = exc
                self._failover_log.append(
                    {"venue": venue.name, "reason": "exception", "err": str(exc), "ts": time.time()}
                )
                logger.warning("venue=%s raised %s; trying fallback", venue.name, exc)
                attempted.append(venue)
                venue = self._fallback_for(venue)

        if last_result is not None:
            venue_name = last_venue_name or self.choose_venue(req.symbol, req.qty, urgency).name
            return last_result.model_copy(update={"raw": {**last_result.raw, "venue": venue_name}})
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("place_with_failover: no venue attempted")

    async def get_order_status(
        self,
        symbol: str,
        order_id: str,
        *,
        venue_name: str | None = None,
    ) -> OrderResult | None:
        """Best-effort lookup across the primary venue and fallbacks."""
        venues: list[VenueBase] = []
        if venue_name:
            chosen = self._venue_by_name(venue_name)
            if chosen is not None:
                venues.append(chosen)
        if not venues:
            venues.append(self.choose_venue(symbol))
            fallback = self._fallback_for(venues[0])
            if fallback is not None:
                venues.append(fallback)
        elif venues[0] is self.bybit:
            fallback = self._fallback_for(self.bybit)
            if fallback is not None:
                venues.append(fallback)
        elif venues[0] is self.tradovate:
            fallback = self._fallback_for(self.tradovate)
            if fallback is not None:
                venues.append(fallback)
        elif venues[0] is self.okx:
            fallback = self._fallback_for(self.okx)
            if fallback is not None:
                venues.append(fallback)
        elif venues[0] is self.ibkr:
            fallback = self._fallback_for(self.ibkr)
            if fallback is not None:
                venues.append(fallback)
        elif venues[0] is self.tastytrade:
            fallback = self._fallback_for(self.tastytrade)
            if fallback is not None:
                venues.append(fallback)
        seen: set[str] = set()
        for venue in venues:
            if venue.name in seen:
                continue
            seen.add(venue.name)
            try:
                result = await venue.get_order_status(symbol, order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("order status lookup failed venue=%s err=%s", venue.name, exc)
                continue
            if result is not None:
                return result.model_copy(update={"raw": {**result.raw, "venue": venue.name}})
        return None

    async def get_order_book(
        self,
        symbol: str,
        *,
        venue_name: str | None = None,
        depth: int = 5,
    ) -> dict[str, object] | None:
        """Best-effort book snapshot across the primary venue and fallbacks."""
        venues: list[VenueBase] = []
        if venue_name:
            chosen = self._venue_by_name(venue_name)
            if chosen is not None:
                venues.append(chosen)
        if not venues:
            venues.append(self.choose_venue(symbol))
            fallback = self._fallback_for(venues[0])
            if fallback is not None:
                venues.append(fallback)
        elif venues[0] is self.bybit:
            fallback = self._fallback_for(self.bybit)
            if fallback is not None:
                venues.append(fallback)
        elif venues[0] is self.tradovate:
            fallback = self._fallback_for(self.tradovate)
            if fallback is not None:
                venues.append(fallback)
        elif venues[0] is self.okx:
            fallback = self._fallback_for(self.okx)
            if fallback is not None:
                venues.append(fallback)
        elif venues[0] is self.ibkr:
            fallback = self._fallback_for(self.ibkr)
            if fallback is not None:
                venues.append(fallback)
        elif venues[0] is self.tastytrade:
            fallback = self._fallback_for(self.tastytrade)
            if fallback is not None:
                venues.append(fallback)
        seen: set[str] = set()
        for venue in venues:
            if venue.name in seen:
                continue
            seen.add(venue.name)
            try:
                result = await venue.get_order_book(symbol, depth=depth)
            except Exception as exc:  # noqa: BLE001
                logger.warning("order book lookup failed venue=%s err=%s", venue.name, exc)
                continue
            if result is not None:
                payload = dict(result)
                payload["venue"] = venue.name
                payload["order_book_venue"] = venue.name
                payload.setdefault("symbol", symbol)
                return payload
        return None
