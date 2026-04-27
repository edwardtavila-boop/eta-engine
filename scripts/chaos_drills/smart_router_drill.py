"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills.smart_router_drill.

Drill: force post-only to "would cross"; verify the router preserves intent.

What this drill asserts
-----------------------
:func:`core.smart_router.route` under ``policy="post_only"`` has two
branches the live session depends on:

* **Strict post-only** (``allow_market_fallback=False``): if the limit
  would cross top-of-book, the router must skip the slice and return
  the full ``total_qty`` as ``remainder_qty`` -- never silently submit
  a crossing order.
* **Fallback-enabled** (``allow_market_fallback=True``): same would-
  cross scenario must emit a single MARKET child order so execution
  still happens.

Silent regressions here would either (a) let a post-only intent leak
into a taker fill (silent fee blow-up) or (b) halt the venue failover
path entirely.

The drill also validates the iceberg reveal path as a sanity regression
gate -- if reveal slicing silently collapses to one big child, size
concentration changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.core.smart_router import ParentOrder, route
from eta_engine.scripts.chaos_drills._common import drill_result

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["drill_smart_router"]


def drill_smart_router(sandbox: Path) -> dict[str, Any]:  # noqa: ARG001
    """Exercise post-only intent-preservation + fallback + iceberg reveal."""
    # Would-cross buy at 101 with best_ask=100. Strict post-only -> no children.
    strict = route(
        ParentOrder(symbol="MNQ", side="buy", total_qty=4.0, limit_price=101.0, allow_market_fallback=False),
        "post_only",
        best_bid=99.0,
        best_ask=100.0,
    )
    if strict.children:
        return drill_result(
            "smart_router",
            passed=False,
            details=f"strict post-only emitted {len(strict.children)} children on a crossing limit",
        )
    if abs(strict.remainder_qty - 4.0) > 1e-9:
        return drill_result(
            "smart_router",
            passed=False,
            details=f"strict post-only remainder_qty {strict.remainder_qty!r} != 4.0",
        )

    # Same scenario with fallback=True -> one MARKET child.
    fallback = route(
        ParentOrder(symbol="MNQ", side="buy", total_qty=4.0, limit_price=101.0, allow_market_fallback=True),
        "post_only",
        best_bid=99.0,
        best_ask=100.0,
    )
    if len(fallback.children) != 1:
        return drill_result(
            "smart_router",
            passed=False,
            details=f"fallback post-only produced {len(fallback.children)} children (want 1)",
        )
    fallback_child = fallback.children[0]
    if fallback_child.order_type != "MARKET":
        return drill_result(
            "smart_router",
            passed=False,
            details=f"fallback child was {fallback_child.order_type}, expected MARKET",
        )

    # Iceberg sanity: total qty 10, reveal 2 -> 5 children of qty 2.
    iceberg = route(
        ParentOrder(symbol="MNQ", side="buy", total_qty=10.0, limit_price=99.5, reveal_size=2.0),
        "iceberg",
    )
    if len(iceberg.children) != 5:
        return drill_result(
            "smart_router",
            passed=False,
            details=f"iceberg reveal slicing produced {len(iceberg.children)} children (want 5)",
        )
    if any(abs(c.qty - 2.0) > 1e-9 for c in iceberg.children):
        return drill_result(
            "smart_router",
            passed=False,
            details=f"iceberg children had wrong qty: {[c.qty for c in iceberg.children]}",
        )

    return drill_result(
        "smart_router",
        passed=True,
        details="strict post-only skipped, fallback produced MARKET, iceberg sliced 5x2",
        observed={
            "strict_children": len(strict.children),
            "strict_remainder": strict.remainder_qty,
            "fallback_type": fallback_child.order_type,
            "iceberg_children": len(iceberg.children),
        },
    )
