"""
EVOLUTIONARY TRADING ALGO  //  obs.vps_redundancy
=====================================
Secondary-VPS failover controller.

Why this exists
---------------
The bot portfolio runs on a primary VPS. If that VPS (or its datacenter,
or the upstream ISP) goes down, positions keep burning costs while the
controller is blind. Pager duty on the operator is fine, but the bots
should be able to hand off to a warm secondary VPS WITHOUT the operator
in the loop.

This module does NOT spin up cloud VMs. It assumes both a primary and a
secondary VPS are already running the stack. The controller's job is to:

  1. Probe the health of primary + secondary (HealthProbe protocol).
  2. Decide the current "active" role based on recent probe history
     (FailoverController.decide).
  3. When the active flips, call a DnsSwitchProvider to update the
     upstream DNS A record (or load balancer, or CloudFront origin),
     so clients start hitting the new active host.

Design
------
* All I/O goes through injectable protocols. Tests run entirely offline
  with ``StubHealthProbe`` + ``StubDnsSwitchProvider``.
* Time is injected via a clock callable so tests are deterministic.
* The controller is async-friendly but does not require an event loop
  for its pure-decision API (``decide``) -- the loop version is a thin
  ``run`` wrapper for production.
* No global state. Each controller owns its own probe history and
  current-active label.

Public API
----------
  * ``VpsRole`` -- PRIMARY / SECONDARY
  * ``VpsHealth`` -- HEALTHY / DEGRADED / DOWN
  * ``HealthSnapshot`` -- one probe result
  * ``HealthProbe`` Protocol + ``StubHealthProbe``
  * ``DnsSwitchProvider`` Protocol + ``StubDnsSwitchProvider``
  * ``FailoverPolicy`` -- thresholds for switch decisions
  * ``FailoverController`` -- the decision engine
  * ``VpsRedundancyController`` -- loop runner
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums + models
# ---------------------------------------------------------------------------


class VpsRole(StrEnum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"


class VpsHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class HealthSnapshot(BaseModel):
    """One probe observation for one host."""

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    role: VpsRole
    health: VpsHealth
    latency_ms: float | None = None
    detail: str = ""


class FailoverEvent(BaseModel):
    """Recorded each time the active role flips."""

    ts: datetime
    from_role: VpsRole
    to_role: VpsRole
    reason: str
    dns_updated: bool = False
    dns_error: str | None = None


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class FailoverPolicy(BaseModel):
    """Thresholds that govern when the controller flips active.

    Both flip directions (primary->secondary on failure, and
    secondary->primary on recovery) use independent counts so we can
    favour fast failover and cautious failback.
    """

    # How many consecutive DOWN/DEGRADED probes on primary before we flip
    # to secondary. Default: 3 (so a single flap doesn't trigger).
    primary_unhealthy_threshold: int = Field(default=3, ge=1)

    # How many consecutive HEALTHY probes on primary before we flip
    # back. Default: 10 (slow to fail back; avoids flap).
    primary_recovery_threshold: int = Field(default=10, ge=1)

    # How many consecutive DOWN probes on secondary before we alert that
    # the entire redundancy layer is blind. This does not flip active.
    secondary_unhealthy_threshold: int = Field(default=5, ge=1)

    # We consider DEGRADED the same as DOWN for the purposes of
    # primary_unhealthy_threshold. Disable this when you want the
    # controller to only flip on full outage.
    degraded_counts_as_unhealthy: bool = True


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class HealthProbe(Protocol):
    """Probes a named VPS and returns a HealthSnapshot."""

    async def probe(self, role: VpsRole) -> HealthSnapshot: ...


@runtime_checkable
class DnsSwitchProvider(Protocol):
    """Points the public hostname at a specific VPS role."""

    async def switch(self, *, to_role: VpsRole, reason: str) -> None: ...


# ---------------------------------------------------------------------------
# Stubs (test + dry-run use)
# ---------------------------------------------------------------------------


class StubHealthProbe:
    """Deterministic probe. Call ``set_health(role, health)`` to steer it."""

    def __init__(self) -> None:
        self._state: dict[VpsRole, VpsHealth] = {
            VpsRole.PRIMARY: VpsHealth.HEALTHY,
            VpsRole.SECONDARY: VpsHealth.HEALTHY,
        }
        self._latency_ms: dict[VpsRole, float] = {
            VpsRole.PRIMARY: 5.0,
            VpsRole.SECONDARY: 5.0,
        }
        self.calls: list[VpsRole] = []

    def set_health(self, role: VpsRole, health: VpsHealth) -> None:
        self._state[role] = health

    def set_latency(self, role: VpsRole, latency_ms: float) -> None:
        self._latency_ms[role] = latency_ms

    async def probe(self, role: VpsRole) -> HealthSnapshot:
        self.calls.append(role)
        return HealthSnapshot(
            role=role,
            health=self._state[role],
            latency_ms=self._latency_ms[role],
            detail=f"stub probe role={role.value}",
        )


class StubDnsSwitchProvider:
    """Records every switch() call. Never blocks, never fails unless told."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.switches: list[tuple[VpsRole, str]] = []
        self.current: VpsRole = VpsRole.PRIMARY

    async def switch(self, *, to_role: VpsRole, reason: str) -> None:
        if self.fail:
            raise RuntimeError(f"stub DNS provider configured to fail (to={to_role.value})")
        self.switches.append((to_role, reason))
        self.current = to_role


# ---------------------------------------------------------------------------
# Decision engine
# ---------------------------------------------------------------------------


ClockFn = Callable[[], datetime]


class FailoverController:
    """Pure decision engine. Feed it HealthSnapshots, get role flips.

    Not async; no I/O. ``VpsRedundancyController`` wraps this with the
    probe + DNS side effects.
    """

    def __init__(
        self,
        *,
        policy: FailoverPolicy | None = None,
        initial_role: VpsRole = VpsRole.PRIMARY,
        history_len: int = 64,
    ) -> None:
        self.policy = policy or FailoverPolicy()
        self.active_role: VpsRole = initial_role
        self._history: deque[HealthSnapshot] = deque(maxlen=history_len)
        self.events: list[FailoverEvent] = []

    # -- observation -------------------------------------------------------

    def observe(self, snapshot: HealthSnapshot) -> None:
        self._history.append(snapshot)

    def _unhealthy(self, h: VpsHealth) -> bool:
        if h == VpsHealth.DOWN:
            return True
        return h == VpsHealth.DEGRADED and self.policy.degraded_counts_as_unhealthy

    def _consecutive_tail(self, role: VpsRole, predicate: Callable[[VpsHealth], bool]) -> int:
        """Count the longest tail-run of snapshots for ``role`` whose
        health satisfies ``predicate``. Stops at the first mismatch."""
        count = 0
        for snap in reversed(self._history):
            if snap.role != role:
                continue
            if predicate(snap.health):
                count += 1
            else:
                break
        return count

    # -- decisions ---------------------------------------------------------

    def should_flip_to_secondary(self) -> bool:
        if self.active_role != VpsRole.PRIMARY:
            return False
        unhealthy_primary = self._consecutive_tail(
            VpsRole.PRIMARY,
            self._unhealthy,
        )
        return unhealthy_primary >= self.policy.primary_unhealthy_threshold

    def should_flip_to_primary(self) -> bool:
        if self.active_role != VpsRole.SECONDARY:
            return False
        healthy_primary = self._consecutive_tail(
            VpsRole.PRIMARY,
            lambda h: h == VpsHealth.HEALTHY,
        )
        return healthy_primary >= self.policy.primary_recovery_threshold

    def secondary_degraded(self) -> bool:
        """True if secondary has been DOWN for N probes; the operator
        should be paged because we have no working fallback."""
        down_secondary = self._consecutive_tail(
            VpsRole.SECONDARY,
            lambda h: h == VpsHealth.DOWN,
        )
        return down_secondary >= self.policy.secondary_unhealthy_threshold

    # -- transitions -------------------------------------------------------

    def decide(self, *, now: datetime) -> FailoverEvent | None:
        """Inspect state and return a FailoverEvent if a flip is needed."""
        if self.should_flip_to_secondary():
            evt = FailoverEvent(
                ts=now,
                from_role=self.active_role,
                to_role=VpsRole.SECONDARY,
                reason=(f"primary unhealthy for >= {self.policy.primary_unhealthy_threshold} probes"),
            )
            self.active_role = VpsRole.SECONDARY
            self.events.append(evt)
            return evt
        if self.should_flip_to_primary():
            evt = FailoverEvent(
                ts=now,
                from_role=self.active_role,
                to_role=VpsRole.PRIMARY,
                reason=(f"primary healthy for >= {self.policy.primary_recovery_threshold} probes"),
            )
            self.active_role = VpsRole.PRIMARY
            self.events.append(evt)
            return evt
        return None


# ---------------------------------------------------------------------------
# Loop runner
# ---------------------------------------------------------------------------


class VpsRedundancyController:
    """End-to-end loop: probe -> decide -> DNS switch -> record.

    Not strictly required (tests drive ``FailoverController`` directly);
    production uses this as the long-running task.
    """

    def __init__(
        self,
        *,
        probe: HealthProbe,
        dns: DnsSwitchProvider,
        policy: FailoverPolicy | None = None,
        initial_role: VpsRole = VpsRole.PRIMARY,
        clock: ClockFn | None = None,
    ) -> None:
        self.probe = probe
        self.dns = dns
        self.controller = FailoverController(
            policy=policy,
            initial_role=initial_role,
        )
        self._clock: ClockFn = clock if clock is not None else (lambda: datetime.now(UTC))

    async def tick(self) -> FailoverEvent | None:
        """One probe cycle: probe both hosts and decide.

        Returns the FailoverEvent (if a flip happened) so caller can
        log/alert. Does NOT raise on probe failure; a probe exception
        is recorded as a DOWN snapshot.
        """
        for role in (VpsRole.PRIMARY, VpsRole.SECONDARY):
            try:
                snap = await self.probe.probe(role)
            except Exception as exc:  # noqa: BLE001
                snap = HealthSnapshot(
                    ts=self._clock(),
                    role=role,
                    health=VpsHealth.DOWN,
                    detail=f"probe raised: {exc!r}",
                )
            self.controller.observe(snap)

        event = self.controller.decide(now=self._clock())
        if event is not None:
            try:
                await self.dns.switch(to_role=event.to_role, reason=event.reason)
                event.dns_updated = True
            except Exception as exc:  # noqa: BLE001
                event.dns_updated = False
                event.dns_error = repr(exc)
        return event

    async def run(self, *, interval_s: float, max_ticks: int | None = None) -> None:
        """Run ``tick`` every ``interval_s`` seconds.

        ``max_ticks`` bounds the loop for tests; None runs forever.
        """
        i = 0
        while max_ticks is None or i < max_ticks:
            await self.tick()
            i += 1
            if max_ticks is not None and i >= max_ticks:
                break
            await asyncio.sleep(interval_s)
