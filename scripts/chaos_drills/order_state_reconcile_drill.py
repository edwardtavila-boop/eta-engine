"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills.order_state_reconcile_drill.

Drill: simulate reconnect divergence; verify the reconciler emits the
correct corrective actions.

What this drill asserts
-----------------------
:class:`core.order_state_reconcile.OrderStateReconciler` is the single
seam that heals local-vs-venue order-state drift after a reconnect.
This drill constructs a worst-case divergence across four axes and
asserts the reconciler emits the exact corrective actions:

* **Silent fill**: local OPEN, venue FILLED -> MARK_FILLED.
* **Silent cancel**: local OPEN, venue CANCELLED -> MARK_CANCELLED.
* **Local ghost**: local OPEN, venue has no record -> MARK_CANCELLED
  (conservative) or RESOLVE_MISSING (non-conservative).
* **Venue orphan**: venue OPEN, local has no record -> ACCEPT_VENUE.

A silent regression here would let the bot double-fill or ignore
venue truth on reconnect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.core.order_state_reconcile import (
    LocalOrder,
    OrderStateReconciler,
    ReconcileActionKind,
    VenueOrder,
)
from eta_engine.scripts.chaos_drills._common import drill_result

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["drill_order_state_reconcile"]


def drill_order_state_reconcile(sandbox: Path) -> dict[str, Any]:  # noqa: ARG001
    """Verify the reconciler emits the expected action per divergence."""
    local = {
        "coid_fill": LocalOrder(
            client_order_id="coid_fill",
            symbol="MNQ",
            status="OPEN",
            qty=1.0,
            filled_qty=0.0,
            venue_order_id="v_fill",
        ),
        "coid_cancel": LocalOrder(
            client_order_id="coid_cancel",
            symbol="MNQ",
            status="OPEN",
            qty=1.0,
            filled_qty=0.0,
            venue_order_id="v_cancel",
        ),
        "coid_ghost": LocalOrder(
            client_order_id="coid_ghost",
            symbol="MNQ",
            status="OPEN",
            qty=1.0,
            filled_qty=0.0,
            venue_order_id=None,
        ),
    }
    venue = {
        "coid_fill": VenueOrder(
            venue_order_id="v_fill",
            client_order_id="coid_fill",
            symbol="MNQ",
            status="FILLED",
            qty=1.0,
            filled_qty=1.0,
        ),
        "coid_cancel": VenueOrder(
            venue_order_id="v_cancel",
            client_order_id="coid_cancel",
            symbol="MNQ",
            status="CANCELLED",
            qty=1.0,
            filled_qty=0.0,
        ),
        "coid_orphan": VenueOrder(
            venue_order_id="v_orphan",
            client_order_id="coid_orphan",
            symbol="MNQ",
            status="OPEN",
            qty=1.0,
            filled_qty=0.0,
        ),
    }
    rec = OrderStateReconciler(conservative=True)
    report = rec.reconcile(local, venue)
    kinds_by_coid = {a.client_order_id: a.kind for a in report.actions}

    expected = {
        "coid_fill": ReconcileActionKind.MARK_FILLED,
        "coid_cancel": ReconcileActionKind.MARK_CANCELLED,
        "coid_ghost": ReconcileActionKind.MARK_CANCELLED,  # conservative=True
        "coid_orphan": ReconcileActionKind.ACCEPT_VENUE,
    }
    mismatches: list[str] = []
    for coid, want in expected.items():
        got = kinds_by_coid.get(coid)
        if got is not want:
            mismatches.append(f"{coid}: expected {want.value}, got {got}")

    if mismatches:
        return drill_result(
            "order_state_reconcile",
            passed=False,
            details="reconciler emitted unexpected action set: " + "; ".join(mismatches),
            observed={k: v.value for k, v in kinds_by_coid.items()},
        )

    # Idempotency: running reconcile again on the *same* inputs must
    # produce the same action kinds so a retry after a partial recovery
    # is safe.
    rerun = rec.reconcile(local, venue)
    rerun_kinds = {a.client_order_id: a.kind for a in rerun.actions}
    if rerun_kinds != kinds_by_coid:
        return drill_result(
            "order_state_reconcile",
            passed=False,
            details="reconciler was not idempotent on re-run",
            observed={
                "first": {k: v.value for k, v in kinds_by_coid.items()},
                "second": {k: v.value for k, v in rerun_kinds.items()},
            },
        )

    return drill_result(
        "order_state_reconcile",
        passed=True,
        details="reconciler produced MARK_FILLED / MARK_CANCELLED / ACCEPT_VENUE as expected and was idempotent",
        observed={
            "actions": {k: v.value for k, v in kinds_by_coid.items()},
            "has_divergence": report.has_divergence,
            "n_actions": len(report.actions),
        },
    )
