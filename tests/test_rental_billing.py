"""
EVOLUTIONARY TRADING ALGO  //  tests.test_rental_billing
============================================
Subscription state-machine coverage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.rental.billing import (
    CYCLE_DAYS,
    BillingCycle,
    EventKind,
    Subscription,
    SubscriptionStatus,
)
from eta_engine.rental.tiers import RentalTier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh(cycle: BillingCycle = BillingCycle.MONTHLY) -> Subscription:
    return Subscription(
        tenant_id="cust_1",
        tier=RentalTier.STARTER,
        cycle=cycle,
    )


# ---------------------------------------------------------------------------
# START_TRIAL
# ---------------------------------------------------------------------------


def test_start_trial_sets_trial_status_and_7_day_window() -> None:
    sub = _fresh()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    sub.apply_event(EventKind.START_TRIAL, now=now)
    assert sub.status is SubscriptionStatus.TRIAL
    assert sub.current_period_end == now + timedelta(days=7)


# ---------------------------------------------------------------------------
# ACTIVATE
# ---------------------------------------------------------------------------


def test_activate_sets_period_end_by_cycle() -> None:
    sub = _fresh(BillingCycle.QUARTERLY)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    sub.apply_event(EventKind.ACTIVATE, now=now)
    assert sub.status is SubscriptionStatus.ACTIVE
    assert sub.current_period_end == now + timedelta(
        days=CYCLE_DAYS[BillingCycle.QUARTERLY],
    )
    assert sub.grace_until is None


def test_activate_clears_prior_grace() -> None:
    sub = _fresh()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    sub.apply_event(EventKind.PAYMENT_FAILED, now=now)
    assert sub.grace_until is not None
    sub.apply_event(EventKind.ACTIVATE, now=now + timedelta(days=1))
    assert sub.grace_until is None


# ---------------------------------------------------------------------------
# RENEWAL_PAID
# ---------------------------------------------------------------------------


def test_renewal_paid_extends_past_existing_period_end() -> None:
    sub = _fresh(BillingCycle.ANNUAL)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    sub.apply_event(EventKind.ACTIVATE, now=t0)
    renew_time = t0 + timedelta(days=30)  # customer renews early
    sub.apply_event(EventKind.RENEWAL_PAID, now=renew_time)
    # Base is old period end (365 out), +365 more -> 730d from t0
    expected = t0 + timedelta(days=365) + timedelta(days=365)
    assert sub.current_period_end == expected


def test_renewal_paid_after_lapse_starts_from_now() -> None:
    sub = _fresh(BillingCycle.MONTHLY)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    sub.apply_event(EventKind.ACTIVATE, now=t0)
    # Renew AFTER period end
    late = t0 + timedelta(days=60)
    sub.apply_event(EventKind.RENEWAL_PAID, now=late)
    assert sub.current_period_end == late + timedelta(days=30)


# ---------------------------------------------------------------------------
# PAYMENT_FAILED + REINSTATE
# ---------------------------------------------------------------------------


def test_payment_failed_transitions_to_grace_with_3_day_window() -> None:
    sub = _fresh()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    sub.apply_event(EventKind.PAYMENT_FAILED, now=now)
    assert sub.status is SubscriptionStatus.GRACE
    assert sub.grace_until == now + timedelta(days=3)


def test_reinstate_after_grace_clears_flag() -> None:
    sub = _fresh()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    sub.apply_event(EventKind.PAYMENT_FAILED, now=now)
    sub.apply_event(EventKind.REINSTATE, now=now + timedelta(days=1))
    assert sub.status is SubscriptionStatus.ACTIVE
    assert sub.grace_until is None


# ---------------------------------------------------------------------------
# CANCEL + EXPIRE
# ---------------------------------------------------------------------------


def test_cancel_marks_cancelled_but_preserves_period_end() -> None:
    sub = _fresh()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    sub.apply_event(EventKind.ACTIVATE, now=t0)
    period_end = sub.current_period_end
    sub.apply_event(EventKind.CANCEL, now=t0 + timedelta(days=5))
    assert sub.status is SubscriptionStatus.CANCELLED
    # Period end must NOT be rolled back -- the customer paid for it
    assert sub.current_period_end == period_end


def test_expire_sets_expired_status() -> None:
    sub = _fresh()
    sub.apply_event(EventKind.EXPIRE)
    assert sub.status is SubscriptionStatus.EXPIRED


# ---------------------------------------------------------------------------
# Entitlement check
# ---------------------------------------------------------------------------


def test_is_entitled_during_grace_window() -> None:
    sub = _fresh()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    sub.apply_event(EventKind.PAYMENT_FAILED, now=now)
    ok, reason = sub.is_entitled(now=now + timedelta(hours=12))
    assert ok
    assert "grace" in reason


def test_is_entitled_fails_after_grace() -> None:
    sub = _fresh()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    sub.apply_event(EventKind.PAYMENT_FAILED, now=now)
    ok, reason = sub.is_entitled(now=now + timedelta(days=4))
    assert not ok
    assert "grace expired" in reason


def test_is_entitled_fails_when_cancelled() -> None:
    sub = _fresh()
    sub.apply_event(EventKind.CANCEL)
    ok, reason = sub.is_entitled()
    assert not ok
    assert "CANCELLED" in reason or "status" in reason


def test_is_entitled_fails_when_expired() -> None:
    sub = _fresh()
    sub.apply_event(EventKind.EXPIRE)
    ok, reason = sub.is_entitled()
    assert not ok
    assert "EXPIRED" in reason or "status" in reason


def test_is_entitled_fails_after_period_end_active() -> None:
    sub = _fresh()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    sub.apply_event(EventKind.ACTIVATE, now=t0)
    ok, reason = sub.is_entitled(now=t0 + timedelta(days=60))
    assert not ok
    assert "period ended" in reason


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


def test_event_log_append_order() -> None:
    sub = _fresh()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    sub.apply_event(EventKind.START_TRIAL, now=t0, note="via stripe webhook")
    sub.apply_event(EventKind.ACTIVATE, now=t0 + timedelta(days=7))
    sub.apply_event(EventKind.PAYMENT_FAILED, now=t0 + timedelta(days=37))
    kinds = [ev.kind for ev in sub.events]
    assert kinds == [
        EventKind.START_TRIAL,
        EventKind.ACTIVATE,
        EventKind.PAYMENT_FAILED,
    ]
    assert sub.events[0].note == "via stripe webhook"


def test_apply_event_returns_self_for_chaining() -> None:
    sub = _fresh()
    returned = sub.apply_event(EventKind.START_TRIAL).apply_event(EventKind.ACTIVATE)
    assert returned is sub
    assert sub.status is SubscriptionStatus.ACTIVE
