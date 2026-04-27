"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.circuit_breaker
=================================================
Rate-gated breaker that trips the Fleet when a burst of failures, cost
spikes, or denials suggests something is wrong.

Why this exists
---------------
A bug in an executor, a flaky network, or an adversarial input pattern
can burn through hundreds of dollars of Opus calls in minutes. The
breaker watches the TaskResult stream and short-circuits the Fleet
before the next call if any guard trips.

Three guard bands:
  * ``FailureGuard``  -- trips on N consecutive failures.
  * ``CostGuard``     -- trips when cumulative cost in a sliding window
                          exceeds ``max_cost_per_minute``.
  * ``DenialGuard``   -- trips on N consecutive JARVIS denials (likely a
                          misconfigured policy or a poisoned envelope).

When any guard trips the breaker enters ``OPEN`` state for
``cooldown_seconds``. ``pre_dispatch`` then raises
``BreakerTripped``; the Fleet middleware turns that into a
``TaskResult`` with ``reason_code='breaker_open'`` so callers see a
structured response instead of an exception in the hot path.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from eta_engine.brain.avengers.base import TaskResult


class BreakerState(StrEnum):
    CLOSED = "CLOSED"  # normal
    OPEN = "OPEN"  # tripped, rejecting dispatches
    HALF_OPEN = "HALF_OPEN"  # cooldown expired, probing


class BreakerTripped(RuntimeError):  # noqa: N818 - name is the public API
    """Raised by ``CircuitBreaker.pre_dispatch`` when breaker is OPEN."""


class BreakerStatus(BaseModel):
    """Snapshot of breaker state. Serializable for dashboards."""

    model_config = ConfigDict(frozen=True)

    state: BreakerState
    tripped_at: datetime | None = None
    reopen_at: datetime | None = None
    consec_failures: int = Field(ge=0, default=0)
    consec_denials: int = Field(ge=0, default=0)
    cost_window_sum: float = Field(ge=0.0, default=0.0)
    last_reason: str = ""


class CircuitBreaker:
    """Single-threaded breaker. One per Fleet.

    Parameters
    ----------
    max_consec_failures
        Trip when this many successive failures land without a success.
    max_consec_denials
        Trip when this many successive JARVIS denials land.
    max_cost_per_minute
        Trip if cost accrued in the last 60s exceeds this. 0 disables.
    cooldown_seconds
        How long we stay OPEN before moving to HALF_OPEN.
    """

    def __init__(
        self,
        *,
        max_consec_failures: int = 10,
        max_consec_denials: int = 5,
        max_cost_per_minute: float = 50.0,
        cooldown_seconds: float = 120.0,
        clock: callable | None = None,
    ) -> None:
        self.max_consec_failures = max_consec_failures
        self.max_consec_denials = max_consec_denials
        self.max_cost_per_minute = max_cost_per_minute
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._state = BreakerState.CLOSED
        self._tripped_at: datetime | None = None
        self._reopen_at: datetime | None = None
        self._consec_failures = 0
        self._consec_denials = 0
        self._cost_window: deque[tuple[datetime, float]] = deque()
        self._last_reason = ""

    # --- public ------------------------------------------------------------

    def pre_dispatch(self) -> None:
        """Called before Fleet.dispatch. Raises BreakerTripped when OPEN."""
        now = self._clock()
        if self._state is BreakerState.OPEN:
            if self._reopen_at is not None and now >= self._reopen_at:
                self._state = BreakerState.HALF_OPEN
            else:
                msg = f"breaker OPEN: {self._last_reason}"
                raise BreakerTripped(msg)

    def record(self, result: TaskResult) -> None:
        """Called after Fleet.dispatch. Updates counters and may trip."""
        now = self._clock()
        # --- cost window ---
        cost = float(result.cost_multiplier or 0.0)
        self._cost_window.append((now, cost))
        window_start = now - timedelta(seconds=60)
        while self._cost_window and self._cost_window[0][0] < window_start:
            self._cost_window.popleft()
        window_sum = sum(c for _, c in self._cost_window)

        # --- consec counters ---
        if result.reason_code == "jarvis_denied":
            self._consec_denials += 1
        else:
            self._consec_denials = 0

        if result.success:
            self._consec_failures = 0
        else:
            self._consec_failures += 1

        # --- check trip conditions ---
        reason: str | None = None
        if self._consec_failures >= self.max_consec_failures:
            reason = f"{self._consec_failures} consecutive failures"
        elif self._consec_denials >= self.max_consec_denials:
            reason = f"{self._consec_denials} consecutive JARVIS denials"
        elif self.max_cost_per_minute > 0 and window_sum > self.max_cost_per_minute:
            reason = f"cost/min={window_sum:.2f} exceeds cap={self.max_cost_per_minute:.2f}"

        if reason is not None:
            self._trip(reason, now)
        elif self._state is BreakerState.HALF_OPEN and result.success:
            # Probe succeeded -- close the breaker.
            self._state = BreakerState.CLOSED
            self._tripped_at = None
            self._reopen_at = None
            self._last_reason = ""

    def reset(self) -> None:
        """Manual close. For operator CLI / tests."""
        self._state = BreakerState.CLOSED
        self._tripped_at = None
        self._reopen_at = None
        self._consec_failures = 0
        self._consec_denials = 0
        self._last_reason = ""

    def status(self) -> BreakerStatus:
        window_sum = sum(c for _, c in self._cost_window)
        return BreakerStatus(
            state=self._state,
            tripped_at=self._tripped_at,
            reopen_at=self._reopen_at,
            consec_failures=self._consec_failures,
            consec_denials=self._consec_denials,
            cost_window_sum=window_sum,
            last_reason=self._last_reason,
        )

    # --- internal ----------------------------------------------------------

    def _trip(self, reason: str, now: datetime) -> None:
        self._state = BreakerState.OPEN
        self._tripped_at = now
        self._reopen_at = now + timedelta(seconds=self.cooldown_seconds)
        self._last_reason = reason


__all__ = [
    "BreakerState",
    "BreakerStatus",
    "BreakerTripped",
    "CircuitBreaker",
]
