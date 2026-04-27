"""Tests for core.order_state_reconcile."""

from __future__ import annotations

from eta_engine.core.order_state_reconcile import (
    LocalOrder,
    OrderStateReconciler,
    ReconcileActionKind,
    VenueOrder,
)


def _local(
    coid: str,
    status: str = "OPEN",
    *,
    qty: float = 1.0,
    filled: float = 0.0,
    venue_id: str | None = None,
    symbol: str = "ES=F",
) -> LocalOrder:
    return LocalOrder(
        client_order_id=coid,
        symbol=symbol,
        status=status,
        qty=qty,
        filled_qty=filled,
        venue_order_id=venue_id,
    )


def _venue(
    coid: str,
    status: str = "OPEN",
    *,
    qty: float = 1.0,
    filled: float = 0.0,
    venue_id: str = "V1",
    symbol: str = "ES=F",
) -> VenueOrder:
    return VenueOrder(
        venue_order_id=venue_id,
        client_order_id=coid,
        symbol=symbol,
        status=status,
        qty=qty,
        filled_qty=filled,
    )


class TestReconcilePairs:
    def test_matched_states_noop(self):
        rec = OrderStateReconciler()
        report = rec.reconcile(
            local={"c1": _local("c1", "OPEN")},
            venue={"c1": _venue("c1", "OPEN")},
        )
        assert len(report.actions) == 1
        assert report.actions[0].kind == ReconcileActionKind.NOOP
        assert report.has_divergence is False

    def test_venue_filled_while_local_open_triggers_mark_filled(self):
        rec = OrderStateReconciler()
        report = rec.reconcile(
            local={"c1": _local("c1", "OPEN", qty=2.0)},
            venue={"c1": _venue("c1", "FILLED", qty=2.0, filled=2.0)},
        )
        assert report.actions[0].kind == ReconcileActionKind.MARK_FILLED
        assert report.actions[0].canonical_filled_qty == 2.0

    def test_venue_partial_while_local_open(self):
        rec = OrderStateReconciler()
        report = rec.reconcile(
            local={"c1": _local("c1", "OPEN", qty=4.0, filled=0.0)},
            venue={"c1": _venue("c1", "PARTIAL", qty=4.0, filled=2.0)},
        )
        assert report.actions[0].kind == ReconcileActionKind.MARK_PARTIAL

    def test_venue_cancelled_while_local_open(self):
        rec = OrderStateReconciler()
        report = rec.reconcile(
            local={"c1": _local("c1", "OPEN")},
            venue={"c1": _venue("c1", "CANCELLED")},
        )
        assert report.actions[0].kind == ReconcileActionKind.MARK_CANCELLED

    def test_venue_rejected_counts_as_cancelled(self):
        rec = OrderStateReconciler()
        report = rec.reconcile(
            local={"c1": _local("c1", "OPEN")},
            venue={"c1": _venue("c1", "REJECTED")},
        )
        assert report.actions[0].kind == ReconcileActionKind.MARK_CANCELLED


class TestPresenceMismatch:
    def test_venue_only_triggers_accept(self):
        rec = OrderStateReconciler()
        report = rec.reconcile(
            local={},
            venue={"c1": _venue("c1", "OPEN")},
        )
        assert report.actions[0].kind == ReconcileActionKind.ACCEPT_VENUE

    def test_local_only_conservative_marks_cancelled(self):
        rec = OrderStateReconciler(conservative=True)
        report = rec.reconcile(
            local={"c1": _local("c1", "OPEN")},
            venue={},
        )
        assert report.actions[0].kind == ReconcileActionKind.MARK_CANCELLED

    def test_local_only_non_conservative_resolves_missing(self):
        rec = OrderStateReconciler(conservative=False)
        report = rec.reconcile(
            local={"c1": _local("c1", "OPEN")},
            venue={},
        )
        assert report.actions[0].kind == ReconcileActionKind.RESOLVE_MISSING

    def test_local_terminal_with_venue_absent_is_noop(self):
        rec = OrderStateReconciler()
        report = rec.reconcile(
            local={"c1": _local("c1", "FILLED", filled=1.0)},
            venue={},
        )
        assert report.actions[0].kind == ReconcileActionKind.NOOP


class TestIdempotency:
    def test_two_runs_produce_same_action_set(self):
        rec = OrderStateReconciler()
        local = {"c1": _local("c1", "OPEN"), "c2": _local("c2", "OPEN")}
        venue = {"c1": _venue("c1", "FILLED", filled=1.0), "c2": _venue("c2", "OPEN")}
        r1 = rec.reconcile(local, venue)
        r2 = rec.reconcile(local, venue)
        kinds_1 = [a.kind for a in r1.actions]
        kinds_2 = [a.kind for a in r2.actions]
        assert kinds_1 == kinds_2


class TestFilterHelpers:
    def test_actions_of_kind_returns_matches_only(self):
        rec = OrderStateReconciler()
        report = rec.reconcile(
            local={
                "a": _local("a", "OPEN"),
                "b": _local("b", "OPEN"),
            },
            venue={
                "a": _venue("a", "FILLED", filled=1.0),
                "b": _venue("b", "OPEN"),
            },
        )
        filled = report.actions_of_kind(ReconcileActionKind.MARK_FILLED)
        assert len(filled) == 1
        assert filled[0].client_order_id == "a"

    def test_has_divergence_true_when_any_nonnoop(self):
        rec = OrderStateReconciler()
        report = rec.reconcile(
            local={"c1": _local("c1", "OPEN")},
            venue={"c1": _venue("c1", "FILLED", filled=1.0)},
        )
        assert report.has_divergence is True

    def test_has_divergence_false_when_all_noop(self):
        rec = OrderStateReconciler()
        report = rec.reconcile(
            local={"c1": _local("c1", "OPEN")},
            venue={"c1": _venue("c1", "OPEN")},
        )
        assert report.has_divergence is False
