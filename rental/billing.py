"""
EVOLUTIONARY TRADING ALGO  //  rental.billing
=================================
Subscription state machine + billing event log for the rental SaaS.

This module is intentionally **pure Python** with no Stripe SDK call. The
payment gateway lives at a higher layer; ``Subscription.apply_event`` is the
transition fn that a webhook handler calls when Stripe fires an invoice /
renewal / cancel event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.rental.tiers import RentalTier


class BillingCycle(StrEnum):
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    ANNUAL = "ANNUAL"


CYCLE_DAYS: dict[BillingCycle, int] = {
    BillingCycle.MONTHLY: 30,
    BillingCycle.QUARTERLY: 91,
    BillingCycle.ANNUAL: 365,
}


class SubscriptionStatus(StrEnum):
    TRIAL = "TRIAL"
    ACTIVE = "ACTIVE"
    GRACE = "GRACE"  # first failed payment -- 3-day grace
    PAST_DUE = "PAST_DUE"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class EventKind(StrEnum):
    START_TRIAL = "START_TRIAL"
    ACTIVATE = "ACTIVATE"
    RENEWAL_PAID = "RENEWAL_PAID"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    REINSTATE = "REINSTATE"
    CANCEL = "CANCEL"
    EXPIRE = "EXPIRE"


@dataclass(frozen=True)
class BillingEvent:
    kind: EventKind
    ts_utc: datetime
    note: str = ""


@dataclass
class Subscription:
    """One subscription record. Thin around a state machine + event log."""

    tenant_id: str
    tier: RentalTier
    cycle: BillingCycle
    status: SubscriptionStatus = SubscriptionStatus.TRIAL
    current_period_end: datetime = field(
        default_factory=lambda: datetime.now(UTC) + timedelta(days=7),
    )
    grace_until: datetime | None = None
    events: list[BillingEvent] = field(default_factory=list)

    # -- helpers ------------------------------------------------------------

    def is_entitled(self, *, now: datetime | None = None) -> tuple[bool, str]:
        now = now if now is not None else datetime.now(UTC)
        if self.status in (SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED):
            return False, f"status={self.status.value}"
        if self.status == SubscriptionStatus.GRACE:
            if self.grace_until is not None and now < self.grace_until:
                return True, "in grace window"
            return False, "grace expired"
        if now >= self.current_period_end and self.status == SubscriptionStatus.ACTIVE:
            return False, "period ended"
        return True, "ok"

    # -- transitions --------------------------------------------------------

    def apply_event(
        self,
        kind: EventKind,
        *,
        now: datetime | None = None,
        note: str = "",
    ) -> Subscription:
        """Mutating transition fn. Returns self for chaining."""
        now = now if now is not None else datetime.now(UTC)
        self.events.append(BillingEvent(kind=kind, ts_utc=now, note=note))

        match kind:
            case EventKind.START_TRIAL:
                self.status = SubscriptionStatus.TRIAL
                self.current_period_end = now + timedelta(days=7)
            case EventKind.ACTIVATE:
                self.status = SubscriptionStatus.ACTIVE
                self.current_period_end = now + timedelta(days=CYCLE_DAYS[self.cycle])
                self.grace_until = None
            case EventKind.RENEWAL_PAID:
                self.status = SubscriptionStatus.ACTIVE
                base = max(now, self.current_period_end)
                self.current_period_end = base + timedelta(days=CYCLE_DAYS[self.cycle])
                self.grace_until = None
            case EventKind.PAYMENT_FAILED:
                self.status = SubscriptionStatus.GRACE
                self.grace_until = now + timedelta(days=3)
            case EventKind.REINSTATE:
                # Successful retry after payment_failed but before expiry.
                self.status = SubscriptionStatus.ACTIVE
                self.grace_until = None
            case EventKind.CANCEL:
                self.status = SubscriptionStatus.CANCELLED
                # Let them ride out the paid period.
            case EventKind.EXPIRE:
                self.status = SubscriptionStatus.EXPIRED
                self.grace_until = None
        return self
