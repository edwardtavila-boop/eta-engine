"""EVOLUTIONARY TRADING ALGO // core.live_shadow.

Paper-fill simulator that walks an L2 book, computes VWAP slippage in bps
vs the mid, and feeds the TCA refit dataset.

This module is the contract surface exercised by
``scripts.chaos_drills.live_shadow_guard_drill``. The drill is the
authoritative behavioural spec:

* Adequate liquidity -> ok=True, size_filled == requested, slippage > 0
  bps when walking past mid for a BUY.
* Exhausted book    -> ok=False, reason="book_exhausted",
  size_filled equal to the sum of available liquidity on the taker side.
* Invalid order     -> ok=False, reason="invalid_order", never raises.

Slippage convention: bps relative to mid, signed so a BUY filled above
mid and a SELL filled below mid both report positive slippage (cost to
the taker). Taker fee bps add directly to slippage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class BookLevel:
    """One depth level of the order book."""

    price: float
    size: float


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """An L2 book snapshot at a single timestamp.

    ``bids`` and ``asks`` are ordered best-to-worst from the taker's
    perspective: best bid first (highest price), best ask first
    (lowest price).
    """

    symbol: str
    venue: str
    ts_iso: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    mid: float


Side = Literal["BUY", "SELL"]


@dataclass(frozen=True, slots=True)
class ShadowOrder:
    """A would-be order routed against a paper book.

    ``regime`` and ``session`` are passed through to the resulting
    fill record so the TCA refit pipeline can stratify slippage.
    """

    symbol: str
    side: Side
    size: float
    requested_px: float
    regime: str = "NORMAL"
    session: str = "RTH"
    taker_fee_bps: float = 0.0


@dataclass(frozen=True, slots=True)
class ShadowFill:
    """Result of a paper fill against a book snapshot."""

    ok: bool
    size_filled: float
    avg_price: float
    slippage_bps: float
    reason: str
    levels_consumed: int = 0
    regime: str = "NORMAL"
    session: str = "RTH"


def _walk_book(
    levels: tuple[BookLevel, ...], size: float
) -> tuple[float, float, int, float]:
    """Greedy VWAP walk.

    Returns (size_filled, vwap, levels_consumed, remaining).
    ``vwap`` is 0.0 when no size was filled.
    """
    remaining = size
    notional = 0.0
    consumed = 0
    filled = 0.0
    for level in levels:
        if remaining <= 0.0:
            break
        if level.size <= 0.0:
            continue
        take = min(remaining, level.size)
        notional += take * level.price
        filled += take
        remaining -= take
        consumed += 1
    vwap = (notional / filled) if filled > 0.0 else 0.0
    return filled, vwap, consumed, remaining


def simulate_fill(order: ShadowOrder, book: BookSnapshot) -> ShadowFill:
    """Walk the book and return a paper fill.

    Never raises. Invalid orders produce a non-ok fill with reason
    ``invalid_order``. Books that cannot absorb the requested size
    produce a non-ok fill with reason ``book_exhausted`` whose
    ``size_filled`` equals the available liquidity on the taker side.
    """
    if (
        order.size <= 0.0
        or order.requested_px <= 0.0
        or book.mid <= 0.0
    ):
        return ShadowFill(
            ok=False,
            size_filled=0.0,
            avg_price=0.0,
            slippage_bps=0.0,
            reason="invalid_order",
            levels_consumed=0,
            regime=order.regime,
            session=order.session,
        )

    if order.side == "BUY":
        levels = book.asks
    elif order.side == "SELL":
        levels = book.bids
    else:
        return ShadowFill(
            ok=False,
            size_filled=0.0,
            avg_price=0.0,
            slippage_bps=0.0,
            reason="invalid_order",
            levels_consumed=0,
            regime=order.regime,
            session=order.session,
        )

    filled, vwap, consumed, remaining = _walk_book(levels, order.size)

    if filled <= 0.0:
        return ShadowFill(
            ok=False,
            size_filled=0.0,
            avg_price=0.0,
            slippage_bps=0.0,
            reason="book_exhausted",
            levels_consumed=0,
            regime=order.regime,
            session=order.session,
        )

    raw_bps = ((vwap - book.mid) / book.mid) * 10_000.0
    signed_bps = raw_bps if order.side == "BUY" else -raw_bps
    slippage_bps = signed_bps + order.taker_fee_bps

    if remaining > 1e-12:
        return ShadowFill(
            ok=False,
            size_filled=filled,
            avg_price=vwap,
            slippage_bps=slippage_bps,
            reason="book_exhausted",
            levels_consumed=consumed,
            regime=order.regime,
            session=order.session,
        )

    return ShadowFill(
        ok=True,
        size_filled=filled,
        avg_price=vwap,
        slippage_bps=slippage_bps,
        reason="ok",
        levels_consumed=consumed,
        regime=order.regime,
        session=order.session,
    )


__all__ = [
    "BookLevel",
    "BookSnapshot",
    "ShadowFill",
    "ShadowOrder",
    "simulate_fill",
]
