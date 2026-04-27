"""
EVOLUTIONARY TRADING ALGO  //  obs.ws_reachability
==================================================
Per-endpoint websocket reachability monitor.

The kill-switch already trips on **explicit** disconnects (TCP RST,
peer close). It does NOT catch the silent-degradation regime --
endpoint stops shipping messages but the socket stays open, or RTT
balloons from 30 ms to 4 s. The runtime keeps trading on stale
quotes until heartbeat-timeout fires (often >60 s).

This module gives every WS-using subsystem a small primitive:

* :class:`EndpointHealth`  -- rolling RTT + last-frame-age tracker.
* :class:`ReachabilityMonitor` -- multi-endpoint registrar; computes
  ``preferred()`` (the lowest-RTT healthy endpoint) and ``status()``
  (full diagnostic snapshot).
* :class:`PrimaryBackupRouter` -- two-endpoint preference helper that
  emits a one-shot ``failover`` event when the active endpoint
  degrades and a healthier alternative exists.

Pure / no-IO. Caller is responsible for:

1. Calling :meth:`EndpointHealth.observe_frame()` whenever a frame is
   received from that endpoint, and :meth:`observe_rtt()` whenever a
   ping completes.
2. Periodically invoking :meth:`ReachabilityMonitor.tick()` to expire
   stale endpoints.
3. Reading :meth:`ReachabilityMonitor.preferred()` to pick where to
   send the next subscription request.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# Health classification
# ---------------------------------------------------------------------------


class EndpointStatus(StrEnum):
    """Coarse health buckets used for routing + alert payloads."""
    HEALTHY     = "HEALTHY"      # RTT below threshold + recent frames
    DEGRADED    = "DEGRADED"     # RTT elevated OR frames slow but not stale
    STALE       = "STALE"        # last frame older than stale_after_seconds
    UNREACHABLE = "UNREACHABLE"  # no successful ping in unreachable_after_seconds


# ---------------------------------------------------------------------------
# EndpointHealth -- one logical websocket peer
# ---------------------------------------------------------------------------


@dataclass
class EndpointHealth:
    """Rolling RTT + last-frame tracker for a single WS endpoint.

    Thread-safety: NOT thread-safe; use one instance per asyncio task.
    """
    name: str
    url: str
    # Tunables (caller may override per-endpoint in ctor):
    rtt_warn_ms:               float = 250.0   # > this -> DEGRADED
    rtt_unreachable_ms:        float = 2000.0  # > this -> UNREACHABLE
    stale_after_seconds:       float = 30.0    # frame-age -> STALE
    unreachable_after_seconds: float = 90.0    # no successful ping -> UNREACHABLE
    rtt_window:                int   = 16      # rolling samples for median RTT
    # State (auto-managed):
    _rtt_samples_ms: list[float] = field(default_factory=list)
    _last_frame_at:  float       = 0.0
    _last_ping_at:   float       = 0.0
    _last_pong_at:   float       = 0.0
    _connected:      bool        = False
    _last_status:    EndpointStatus = EndpointStatus.UNREACHABLE

    # ------------------------------------------------------------------
    # State updates -- caller invokes from real WS callbacks
    # ------------------------------------------------------------------
    def observe_connected(self, now: float | None = None) -> None:
        """Mark the WS as having opened a TCP+upgrade connection."""
        self._connected = True
        # Do not touch frame/ping timestamps; they update on real activity.
        _ = now  # accepted for caller-friendly API symmetry

    def observe_disconnected(self) -> None:
        """Mark the WS as having lost its connection."""
        self._connected = False

    def observe_frame(self, now: float | None = None) -> None:
        """Record that a frame arrived right now (caller passes wall clock)."""
        self._last_frame_at = now if now is not None else time.time()

    def observe_ping_sent(self, now: float | None = None) -> None:
        """Caller is about to send a heartbeat ping; remember when."""
        self._last_ping_at = now if now is not None else time.time()

    def observe_pong(self, now: float | None = None) -> None:
        """A heartbeat pong came back; compute RTT from last_ping_at."""
        n = now if now is not None else time.time()
        self._last_pong_at = n
        if self._last_ping_at > 0.0:
            rtt_ms = max(0.0, (n - self._last_ping_at) * 1000.0)
            self._rtt_samples_ms.append(rtt_ms)
            if len(self._rtt_samples_ms) > self.rtt_window:
                self._rtt_samples_ms = self._rtt_samples_ms[-self.rtt_window:]

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------
    def median_rtt_ms(self) -> float | None:
        if not self._rtt_samples_ms:
            return None
        s = sorted(self._rtt_samples_ms)
        mid = len(s) // 2
        if len(s) % 2 == 1:
            return s[mid]
        return (s[mid - 1] + s[mid]) / 2.0

    def frame_age_seconds(self, now: float | None = None) -> float:
        n = now if now is not None else time.time()
        if self._last_frame_at == 0.0:
            return float("inf")
        return n - self._last_frame_at

    def ping_age_seconds(self, now: float | None = None) -> float:
        n = now if now is not None else time.time()
        if self._last_pong_at == 0.0:
            return float("inf")
        return n - self._last_pong_at

    def status(self, now: float | None = None) -> EndpointStatus:
        """Compute a coarse health bucket; cache as ``_last_status``."""
        n = now if now is not None else time.time()
        if not self._connected:
            self._last_status = EndpointStatus.UNREACHABLE
            return self._last_status
        if self.ping_age_seconds(n) >= self.unreachable_after_seconds:
            self._last_status = EndpointStatus.UNREACHABLE
            return self._last_status
        if self.frame_age_seconds(n) >= self.stale_after_seconds:
            self._last_status = EndpointStatus.STALE
            return self._last_status
        rtt = self.median_rtt_ms()
        if rtt is not None and rtt >= self.rtt_unreachable_ms:
            self._last_status = EndpointStatus.UNREACHABLE
            return self._last_status
        if rtt is not None and rtt >= self.rtt_warn_ms:
            self._last_status = EndpointStatus.DEGRADED
            return self._last_status
        self._last_status = EndpointStatus.HEALTHY
        return self._last_status

    def snapshot(self, now: float | None = None) -> dict[str, object]:
        n = now if now is not None else time.time()
        return {
            "name":              self.name,
            "url":               self.url,
            "status":            self.status(n).value,
            "connected":         self._connected,
            "rtt_ms_median":     self.median_rtt_ms(),
            "rtt_samples":       len(self._rtt_samples_ms),
            "frame_age_seconds": self.frame_age_seconds(n),
            "ping_age_seconds":  self.ping_age_seconds(n),
        }


# ---------------------------------------------------------------------------
# ReachabilityMonitor -- collection of endpoints + selection
# ---------------------------------------------------------------------------


class ReachabilityMonitor:
    """Registrar of multiple :class:`EndpointHealth` instances.

    Use one ``ReachabilityMonitor`` per *protocol* (one for crypto WS,
    one for futures WS, etc.).
    """

    def __init__(self) -> None:
        self._endpoints: dict[str, EndpointHealth] = {}

    def register(self, endpoint: EndpointHealth) -> EndpointHealth:
        self._endpoints[endpoint.name] = endpoint
        return endpoint

    def get(self, name: str) -> EndpointHealth | None:
        return self._endpoints.get(name)

    def all(self) -> list[EndpointHealth]:
        return list(self._endpoints.values())

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def preferred(self, now: float | None = None) -> EndpointHealth | None:
        """Return the lowest-RTT HEALTHY endpoint, or ``None`` if all are bad.

        Tie-break: most recently observed frame.
        """
        n = now if now is not None else time.time()
        healthy = [e for e in self._endpoints.values()
                   if e.status(n) == EndpointStatus.HEALTHY]
        if not healthy:
            return None
        # Sort by (median_rtt_or_inf, -frame_age) so smallest RTT wins,
        # then most-recent-frame wins as tie-break.
        def _key(e: EndpointHealth) -> tuple[float, float]:
            rtt = e.median_rtt_ms()
            return (rtt if rtt is not None else float("inf"),
                    e.frame_age_seconds(n))
        healthy.sort(key=_key)
        return healthy[0]

    def degraded(self, now: float | None = None) -> list[EndpointHealth]:
        """Return endpoints whose status is anything other than HEALTHY."""
        n = now if now is not None else time.time()
        return [e for e in self._endpoints.values()
                if e.status(n) != EndpointStatus.HEALTHY]

    def snapshot(self, now: float | None = None) -> dict[str, object]:
        n = now if now is not None else time.time()
        endpoints = [e.snapshot(n) for e in self._endpoints.values()]
        preferred = self.preferred(n)
        return {
            "preferred":   preferred.name if preferred else None,
            "endpoints":   endpoints,
            "all_healthy": all(
                e["status"] == EndpointStatus.HEALTHY.value for e in endpoints
            ) if endpoints else False,
        }


# ---------------------------------------------------------------------------
# PrimaryBackupRouter -- two-endpoint helper with one-shot failover event
# ---------------------------------------------------------------------------


@dataclass
class FailoverEvent:
    """Emitted by :meth:`PrimaryBackupRouter.poll` on active-endpoint change."""
    from_name: str
    to_name:   str
    reason:    str  # short human string, e.g. "primary STALE; backup HEALTHY"
    at:        float


class PrimaryBackupRouter:
    """Maintain a current-active endpoint with sticky-but-swappable choice.

    Sticky semantics: do NOT bounce back to the primary the instant it
    recovers; require it to be HEALTHY for ``recovery_grace_seconds``
    before swapping back. This avoids flap-routing across a marginal
    network bump.
    """

    def __init__(
        self,
        primary: EndpointHealth,
        backup:  EndpointHealth,
        recovery_grace_seconds: float = 60.0,
    ) -> None:
        self.primary = primary
        self.backup  = backup
        self.recovery_grace_seconds = recovery_grace_seconds
        self._active_name = primary.name
        # When the primary first reaches HEALTHY *while we're on backup*,
        # we record the moment so we can wait recovery_grace before swap-back.
        self._primary_recovered_at: float | None = None

    def active(self) -> EndpointHealth:
        return self.primary if self._active_name == self.primary.name else self.backup

    def poll(self, now: float | None = None) -> FailoverEvent | None:
        """Run the routing decision. Returns a FailoverEvent on swap."""
        n = now if now is not None else time.time()
        active = self.active()
        primary_status = self.primary.status(n)
        backup_status  = self.backup.status(n)

        # Currently on primary:
        if self._active_name == self.primary.name:
            # Stay on primary unless primary degraded AND backup is healthy.
            if primary_status != EndpointStatus.HEALTHY \
                    and backup_status == EndpointStatus.HEALTHY:
                self._active_name = self.backup.name
                self._primary_recovered_at = None
                return FailoverEvent(
                    from_name=self.primary.name,
                    to_name=self.backup.name,
                    reason=f"primary {primary_status.value}; "
                           f"backup {backup_status.value}",
                    at=n,
                )
            return None

        # Currently on backup:
        if primary_status == EndpointStatus.HEALTHY:
            if self._primary_recovered_at is None:
                self._primary_recovered_at = n
            elif (n - self._primary_recovered_at) >= self.recovery_grace_seconds:
                # Sticky-grace satisfied; swap back.
                from_name = self.backup.name
                self._active_name = self.primary.name
                self._primary_recovered_at = None
                return FailoverEvent(
                    from_name=from_name,
                    to_name=self.primary.name,
                    reason="primary stable HEALTHY past grace window",
                    at=n,
                )
        else:
            # Primary still bad -> reset the recovery clock.
            self._primary_recovered_at = None
        _ = active  # explicit no-op; active() retrieved above for symmetry
        return None
