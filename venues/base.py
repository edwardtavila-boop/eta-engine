"""
EVOLUTIONARY TRADING ALGO  //  venues.base
==============================
VenueBase contract + order request/result models.
Every execution surface subclasses this. No direct SDK leaks upstream.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    POST_ONLY = "POST_ONLY"


class OrderStatus(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    OPEN = "OPEN"
    REJECTED = "REJECTED"


class ConnectionStatus(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    STUBBED = "STUBBED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class BracketStyle(StrEnum):
    """How protective stop/target legs are attached to an entry order.

    The two real-world execution styles for our active venues:

    * ``SERVER_OCO`` — the broker accepts a single ``place_order`` that
      registers the parent + STP child + LMT child as an OCO group on
      the broker side. If the supervisor process crashes between the
      parent fill and any subsequent code, the protective legs are
      already at the broker. This is the IBKR live-paper futures path
      (``_build_futures_bracket_orders``) and Alpaca's equity bracket
      via ``order_class=bracket``.

    * ``SUPERVISOR_LOCAL`` — the broker rejects advanced order classes
      on this asset (e.g. Alpaca crypto returns HTTP 422 for any
      non-``simple`` order_class). The entry ships as a plain market /
      limit order; the supervisor's ``_maybe_exit`` tick-level loop
      watches each bar and submits a ``reduce_only`` exit when the
      live price pierces the stop or target. Stop/target are still
      required on the OrderRequest so the supervisor knows where to
      bail; they are NOT shipped to the broker.

    A venue picks the style per (asset_class, symbol) via
    ``bracket_style_for(symbol)``. The supervisor uses this to decide
    whether to enable its tick-level exit watch for that bot.
    """
    SERVER_OCO = "server_oco"
    SUPERVISOR_LOCAL = "supervisor_local"


class ExecutionCapabilities(BaseModel):
    """What an order path supports for one (venue, asset_class) pairing.

    Surfaces the capabilities the supervisor needs to know to drive
    correct lifecycle behavior. Each field mirrors a real broker-side
    constraint we have already hit live:

    * ``bracket_style`` — server-side OCO vs supervisor-local. See
      :class:`BracketStyle`.
    * ``min_cost_basis_usd`` — server-side notional minimum (Alpaca
      crypto: $10; futures: 0; equity: 0). When non-zero, the venue
      pre-checks ``qty * limit_price`` and rejects below threshold
      with a deterministic reason.
    * ``min_order_qty`` — fractional minimum (Alpaca crypto BTC:
      0.000012437 from /v2/assets/BTC%2FUSD; futures: 1).
    * ``supports_reduce_only`` — whether the broker honors the
      ``reduce_only`` flag on an order. False means the supervisor
      must compute the exit qty against current position before
      submission (we do this regardless for safety, but this field
      tells callers when the broker will silently bypass it).
    * ``supports_session_aware_routing`` — ``True`` when the venue
      respects the asset's primary session window for MARKET-to-LIMIT
      conversion (IBKR via ``_in_primary_session``). Crypto venues
      where the market is 24/7 should report ``False``.
    """
    bracket_style: BracketStyle
    min_cost_basis_usd: float = 0.0
    min_order_qty: float = 0.0
    supports_reduce_only: bool = True
    supports_session_aware_routing: bool = False


class OrderRequest(BaseModel):
    """Venue-agnostic order spec."""

    symbol: str
    side: Side
    qty: float = Field(gt=0.0)
    order_type: OrderType = OrderType.MARKET
    price: float | None = None
    reduce_only: bool = False
    client_order_id: str | None = None
    # Bracket attachment.  When BOTH populated the venue MUST place a
    # parent + STP child + LMT child as a single OCO group.  Naked
    # entries (stop_price=None) on a non-reduce-only request are rejected
    # by the venue layer — see ibkr_live.place_order.  Exits use
    # reduce_only=True and bypass the bracket requirement.
    stop_price: float | None = None
    target_price: float | None = None
    bot_id: str | None = None


class OrderResult(BaseModel):
    """Venue-agnostic order execution result."""

    order_id: str
    status: OrderStatus
    filled_qty: float = 0.0
    avg_price: float = 0.0
    fees: float = 0.0
    latency_ms: float = 0.0
    raw: dict[str, Any] = Field(default_factory=dict)


@dataclass
class VenueConnectionReport:
    """Best-effort connection probe for a venue or broker alias."""

    venue: str
    status: ConnectionStatus
    creds_present: bool
    balance: dict[str, float] = field(default_factory=dict)
    positions_count: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    connected_utc: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["connected_utc"] = self.connected_utc.isoformat()
        return payload


class VenueBase(ABC):
    """Abstract trading venue surface."""

    name: str = "base"

    def __init__(self, api_key: str = "", api_secret: str = "") -> None:
        self.api_key = api_key
        self.api_secret = api_secret

    def has_credentials(self) -> bool:
        """Return True when the venue has enough secrets to attempt auth."""
        return bool(self.api_key) and bool(self.api_secret)

    def connection_endpoint(self) -> str | None:
        """Return the human-readable endpoint used for connection probes."""
        return None

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...

    @abstractmethod
    async def get_positions(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_balance(self) -> dict[str, float]: ...

    async def get_order_status(self, symbol: str, order_id: str) -> OrderResult | None:
        """Best-effort order lookup for live reconciliation."""
        _ = symbol, order_id
        return None

    def execution_capabilities_for(self, symbol: str) -> ExecutionCapabilities:
        """Per-(venue, symbol) execution capability surface.

        Default returns a conservative SERVER_OCO profile (matches
        IBKR futures behavior). Venues with mixed asset classes — like
        Alpaca, where equity supports server-side bracket but crypto
        does not — override this to inspect the symbol and branch on
        asset class.

        The supervisor consults this to decide:
          * Whether to enable its tick-level ``_maybe_exit`` watch for
            that bot (only when bracket_style == SUPERVISOR_LOCAL).
          * Whether to pre-check qty against ``min_cost_basis_usd``
            and ``min_order_qty`` before submission.
          * Whether to apply session-aware MARKET-to-LIMIT
            conversion (IBKR-style RTH window check).

        Default keeps existing behavior intact for venues that haven't
        opted in yet.
        """
        _ = symbol
        return ExecutionCapabilities(bracket_style=BracketStyle.SERVER_OCO)

    async def get_order_book(self, symbol: str, depth: int = 5) -> dict[str, Any] | None:
        """Best-effort top-of-book snapshot for live microstructure enrichment."""
        _ = symbol, depth
        return None

    async def connect(self) -> VenueConnectionReport:
        """Run a safe connection probe for operators and automation.

        The default implementation never places orders. It tries the
        read-only balance and position probes, then labels the result
        using the current credential state.
        """
        endpoint = self.connection_endpoint()
        creds_present = self.has_credentials()
        balance: dict[str, float] = {}
        positions: list[dict[str, Any]] = []
        errors: list[str] = []

        try:
            balance = await self.get_balance()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"balance:{type(exc).__name__}:{exc}")

        try:
            positions = await self.get_positions()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"positions:{type(exc).__name__}:{exc}")

        if errors:
            status = ConnectionStatus.DEGRADED if balance or positions else ConnectionStatus.FAILED
        elif creds_present:
            status = ConnectionStatus.READY
        else:
            status = ConnectionStatus.STUBBED

        details: dict[str, Any] = {}
        if endpoint:
            details["endpoint"] = endpoint
        if errors:
            details["errors"] = errors
        return VenueConnectionReport(
            venue=self.name,
            status=status,
            creds_present=creds_present,
            balance=balance,
            positions_count=len(positions),
            details=details,
            error="; ".join(errors),
        )

    def idempotency_key(self, request: OrderRequest) -> str:
        """Deterministic client order id from the request payload."""
        if request.client_order_id:
            return request.client_order_id
        payload = "|".join(
            [
                self.name,
                request.symbol,
                request.side.value,
                f"{request.qty:.8f}",
                request.order_type.value,
                f"{request.price or 0.0:.8f}",
                str(request.reduce_only),
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:32]
