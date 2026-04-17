"""Iceberg / TWAP / Post-only smart order router — P5_EXEC smart_router.

Splits a single parent order into a sequence of child orders under one of
three policies:

* **Iceberg** — only show ``reveal_size`` contracts at a time. Refill on
  child-fill until the parent is exhausted.
* **TWAP** — slice into ``N`` equal children spaced evenly across
  ``duration_seconds``.
* **Post-only** — refuse to cross the spread. If the passive quote would
  cross, abort the child slice and leave remainder for the next tick.

This module is pure-python — it returns a scheduler plan (list of child
orders + timestamps). The venue adapter consumes the plan and places the
orders via its usual async submit path.

Ideas here follow the FIX-style parent/child pattern used in Tradovate and
Bybit; venue-specific quirks (iceberg hidden liquidity on Bybit, Tradovate
stop-limit child) are handled by the adapter.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

Side = Literal["buy", "sell"]
RoutePolicy = Literal["iceberg", "twap", "post_only"]


class ParentOrder(BaseModel):
    """The user-submitted order that gets sliced."""

    symbol: str
    side: Side
    total_qty: float
    limit_price: float | None = None
    # Per-policy knobs
    reveal_size: float | None = None  # iceberg reveal
    num_slices: int | None = None  # TWAP slice count
    duration_seconds: int | None = None  # TWAP window
    allow_market_fallback: bool = False  # post_only may skip slice; this flag converts remaining


class ChildOrder(BaseModel):
    """One slice emitted by the router."""

    parent_symbol: str
    side: Side
    qty: float
    limit_price: float | None
    order_type: Literal["LIMIT", "MARKET", "POST_ONLY"] = "LIMIT"
    scheduled_ts: datetime
    slice_index: int


class RoutingPlan(BaseModel):
    """Output of :func:`route` — downstream venue executor iterates this."""

    parent_symbol: str
    policy: RoutePolicy
    children: list[ChildOrder] = Field(default_factory=list)
    remainder_qty: float = 0.0
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

def _route_iceberg(
    parent: ParentOrder,
    now: datetime,
) -> RoutingPlan:
    reveal = parent.reveal_size if parent.reveal_size is not None else parent.total_qty / 4.0
    if reveal <= 0:
        raise ValueError("iceberg reveal_size must be positive")
    remaining = parent.total_qty
    children: list[ChildOrder] = []
    idx = 0
    # Iceberg children are all scheduled at t=0; the venue refills them
    # sequentially as fills come in. We just list them so the executor has
    # a full queue to work against.
    while remaining > 1e-9:
        qty = min(reveal, remaining)
        children.append(ChildOrder(
            parent_symbol=parent.symbol,
            side=parent.side,
            qty=round(qty, 8),
            limit_price=parent.limit_price,
            order_type="LIMIT",
            scheduled_ts=now,
            slice_index=idx,
        ))
        remaining -= qty
        idx += 1
    return RoutingPlan(
        parent_symbol=parent.symbol,
        policy="iceberg",
        children=children,
        remainder_qty=0.0,
        notes=[f"iceberg reveal={reveal}"],
    )


def _route_twap(
    parent: ParentOrder,
    now: datetime,
) -> RoutingPlan:
    slices = parent.num_slices or 10
    duration = parent.duration_seconds or 600
    if slices <= 0 or duration <= 0:
        raise ValueError("twap num_slices + duration_seconds must be positive")
    slice_qty = parent.total_qty / slices
    gap = duration / slices
    children = [
        ChildOrder(
            parent_symbol=parent.symbol,
            side=parent.side,
            qty=round(slice_qty, 8),
            limit_price=parent.limit_price,
            order_type="LIMIT" if parent.limit_price else "MARKET",
            scheduled_ts=now + timedelta(seconds=gap * i),
            slice_index=i,
        )
        for i in range(slices)
    ]
    return RoutingPlan(
        parent_symbol=parent.symbol,
        policy="twap",
        children=children,
        remainder_qty=0.0,
        notes=[f"twap slices={slices} gap={gap:.1f}s"],
    )


def _route_post_only(
    parent: ParentOrder,
    now: datetime,
    best_bid: float | None,
    best_ask: float | None,
) -> RoutingPlan:
    if parent.limit_price is None:
        raise ValueError("post_only requires an explicit limit_price")
    notes: list[str] = []
    # For a buy, price must be <= best_bid (makes, doesn't cross)
    # For a sell, price must be >= best_ask
    would_cross = False
    if parent.side == "buy" and best_ask is not None and parent.limit_price >= best_ask:
        would_cross = True
    if parent.side == "sell" and best_bid is not None and parent.limit_price <= best_bid:
        would_cross = True

    if would_cross and not parent.allow_market_fallback:
        notes.append("limit would cross top-of-book — skipping to preserve post-only intent")
        return RoutingPlan(
            parent_symbol=parent.symbol,
            policy="post_only",
            children=[],
            remainder_qty=parent.total_qty,
            notes=notes,
        )
    order_type: Literal["POST_ONLY", "MARKET"] = "POST_ONLY"
    if would_cross and parent.allow_market_fallback:
        order_type = "MARKET"
        notes.append("would cross → market fallback invoked")
    children = [ChildOrder(
        parent_symbol=parent.symbol,
        side=parent.side,
        qty=parent.total_qty,
        limit_price=parent.limit_price,
        order_type=order_type,
        scheduled_ts=now,
        slice_index=0,
    )]
    return RoutingPlan(
        parent_symbol=parent.symbol,
        policy="post_only",
        children=children,
        remainder_qty=0.0,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def route(
    parent: ParentOrder,
    policy: RoutePolicy,
    *,
    now: datetime | None = None,
    best_bid: float | None = None,
    best_ask: float | None = None,
) -> RoutingPlan:
    """Produce a :class:`RoutingPlan` for ``parent`` under ``policy``.

    Parameters
    ----------
    parent
        Canonical parent order.
    policy
        One of ``"iceberg"``, ``"twap"``, ``"post_only"``.
    now
        Anchor timestamp for scheduling (defaults to utcnow).
    best_bid, best_ask
        Top-of-book snapshot. Only required for post-only routing.
    """
    if parent.total_qty <= 0:
        raise ValueError(f"parent.total_qty must be positive, got {parent.total_qty}")
    t0 = now or datetime.now(UTC)
    logger.info("smart_router.route | %s %s qty=%s policy=%s", parent.symbol, parent.side, parent.total_qty, policy)
    if policy == "iceberg":
        return _route_iceberg(parent, t0)
    if policy == "twap":
        return _route_twap(parent, t0)
    if policy == "post_only":
        return _route_post_only(parent, t0, best_bid, best_ask)
    raise ValueError(f"unknown policy {policy!r}")
