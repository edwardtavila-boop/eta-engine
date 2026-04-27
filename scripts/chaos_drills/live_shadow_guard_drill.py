"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills.live_shadow_guard_drill.

Drill: diverge live vs simulated fills; verify slippage is reported.

What this drill asserts
-----------------------
:func:`core.live_shadow.simulate_fill` is the paper-fill simulator feeding
the TCA refit dataset. A silent regression would be one of:

* **Silent full-fill** on an exhausted book: returns ``ok=True`` with a
  smaller ``size_filled`` than requested.
* **Zero slippage** on a thin book: walk-the-book VWAP matches mid
  price despite multiple levels consumed.
* **Crash path**: invalid order (zero size / zero price) should return
  a non-ok fill with reason ``invalid_order``, not raise.

The drill builds two books:

1. **Adequate liquidity**: BUY of 3 at requested=100.0 with asks at
   100.01 (5 lots). Expected: full fill, slippage > 0 bps (moved up
   past mid), ok=True.
2. **Exhausted book**: BUY of 10 at requested=100.0 with asks summing
   to only 4 lots. Expected: partial fill, ok=False, reason contains
   ``book_exhausted``.

Plus the invalid-order path that must never raise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.core.live_shadow import (
    BookLevel,
    BookSnapshot,
    ShadowOrder,
    simulate_fill,
)
from eta_engine.scripts.chaos_drills._common import drill_result

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["drill_live_shadow_guard"]


def drill_live_shadow_guard(sandbox: Path) -> dict[str, Any]:  # noqa: ARG001
    """Exercise full-fill, exhausted-book, and invalid-order branches."""
    ts = "2026-04-24T00:00:00Z"

    # 1. Adequate liquidity: 3 contracts absorbed at 100.01 (5-lot level).
    adequate_book = BookSnapshot(
        symbol="MNQ",
        venue="ibkr",
        ts_iso=ts,
        bids=(BookLevel(99.99, 5.0), BookLevel(99.98, 10.0)),
        asks=(BookLevel(100.01, 5.0), BookLevel(100.03, 10.0)),
        mid=100.0,
    )
    adequate_order = ShadowOrder(
        symbol="MNQ",
        side="BUY",
        size=3.0,
        requested_px=100.0,
        regime="NORMAL",
        session="RTH",
        taker_fee_bps=2.0,
    )
    ok_fill = simulate_fill(adequate_order, adequate_book)
    if not ok_fill.ok or abs(ok_fill.size_filled - 3.0) > 1e-9:
        return drill_result(
            "live_shadow_guard",
            passed=False,
            details=(f"adequate-book fill did not complete: ok={ok_fill.ok} filled={ok_fill.size_filled}"),
        )
    if ok_fill.slippage_bps <= 0.0:
        return drill_result(
            "live_shadow_guard",
            passed=False,
            details=f"adequate-book BUY had non-positive slippage: {ok_fill.slippage_bps!r}",
        )

    # 2. Exhausted book: BUY of 10 against only 4 lots of asks.
    thin_book = BookSnapshot(
        symbol="MNQ",
        venue="ibkr",
        ts_iso=ts,
        bids=(BookLevel(99.99, 2.0),),
        asks=(BookLevel(100.01, 1.0), BookLevel(100.05, 3.0)),
        mid=100.0,
    )
    fat_order = ShadowOrder(
        symbol="MNQ",
        side="BUY",
        size=10.0,
        requested_px=100.0,
        regime="STRESS",
        session="RTH",
    )
    partial = simulate_fill(fat_order, thin_book)
    if partial.ok:
        return drill_result(
            "live_shadow_guard",
            passed=False,
            details=f"exhausted-book fill reported ok=True (should be False), reason={partial.reason!r}",
        )
    if abs(partial.size_filled - 4.0) > 1e-9:
        return drill_result(
            "live_shadow_guard",
            passed=False,
            details=f"exhausted-book size_filled={partial.size_filled} (expected 4.0)",
        )
    if partial.reason != "book_exhausted":
        return drill_result(
            "live_shadow_guard",
            passed=False,
            details=f"exhausted-book reason={partial.reason!r} (expected 'book_exhausted')",
        )

    # 3. Invalid-order path: must return a non-ok fill, never raise.
    bad_order = ShadowOrder(
        symbol="MNQ",
        side="BUY",
        size=0.0,
        requested_px=100.0,
        regime="NORMAL",
        session="RTH",
    )
    bad_fill = simulate_fill(bad_order, adequate_book)
    if bad_fill.ok or bad_fill.reason != "invalid_order":
        return drill_result(
            "live_shadow_guard",
            passed=False,
            details=f"invalid-order fill: ok={bad_fill.ok} reason={bad_fill.reason!r}",
        )

    return drill_result(
        "live_shadow_guard",
        passed=True,
        details=(
            "adequate-book full-fill emitted positive slippage; exhausted book flagged partial; invalid order absorbed"
        ),
        observed={
            "ok_slippage_bps": round(ok_fill.slippage_bps, 4),
            "partial_filled": partial.size_filled,
            "partial_reason": partial.reason,
            "invalid_reason": bad_fill.reason,
        },
    )
