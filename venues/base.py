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


class OrderRequest(BaseModel):
    """Venue-agnostic order spec."""

    symbol: str
    side: Side
    qty: float = Field(gt=0.0)
    order_type: OrderType = OrderType.MARKET
    price: float | None = None
    reduce_only: bool = False
    client_order_id: str | None = None


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
