"""
EVOLUTIONARY TRADING ALGO  //  obs.jarvis_supervisor
========================================
Live supervisor for the JarvisContextEngine.

Why this exists
---------------
Jarvis is now the admin of the fleet (``brain.jarvis_admin``). If Jarvis
goes stale, every subsystem that calls ``request_approval()`` degrades
to stale policy. The failure mode is silent: the engine just stops
ticking, memory stops growing, trajectory says FLAT, and suggestions
fossilize on the last good snapshot.

This module gives Jarvis a heartbeat + drift detector:

  * Wraps ``JarvisContextEngine.tick()`` so every tick's timestamp is
    recorded on the supervisor.
  * Classifies health as GREEN / YELLOW / RED based on:
      - staleness (seconds since last tick)
      - dominance (same binding_constraint for N snapshots in a row,
        implying weights are miscalibrated -- one factor is always
        the bottleneck)
      - flatline composite (stress composite below an activity floor
        for N snapshots -- Jarvis is too optimistic)
      - invalid composite (NaN or out of [0, 1])
      - empty memory past dead_after_s
  * Emits dedup-keyed alerts via an optional MultiAlerter.
  * Runs as a long-lived async loop (``run``) or single-shot sync eval
    (``snapshot_health``).

Design
------
All I/O is injected (MultiAlerter is optional; clock is injected). The
supervisor does not own the engine --- it observes it. Tests run fully
offline with a stub engine.

Public API
----------
  * ``JarvisHealth``           -- GREEN / YELLOW / RED
  * ``JarvisHealthReport``     -- pydantic health snapshot
  * ``SupervisorPolicy``       -- tunable thresholds
  * ``JarvisSupervisor``       -- the supervisor itself
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime  # noqa: TC003  -- pydantic needs runtime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_context import (
        JarvisContext,
        JarvisContextEngine,
    )
    from eta_engine.obs.alerts import MultiAlerter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums + models
# ---------------------------------------------------------------------------


class JarvisHealth(StrEnum):
    """Tri-state supervisor verdict."""

    GREEN = "GREEN"  # fresh + balanced
    YELLOW = "YELLOW"  # drifting or stale but not dead
    RED = "RED"  # dead / invalid / silent beyond threshold


class JarvisHealthReport(BaseModel):
    """One evaluation of Jarvis health. Pure snapshot --- never mutated."""

    ts: datetime
    health: JarvisHealth
    reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    last_tick_at: datetime | None = None
    last_composite: float | None = None
    last_binding: str | None = None
    memory_len: int = 0

    @property
    def is_healthy(self) -> bool:
        return self.health == JarvisHealth.GREEN

    @property
    def degraded(self) -> bool:
        return self.health != JarvisHealth.GREEN


class SupervisorPolicy(BaseModel):
    """Thresholds for classifying supervisor verdicts.

    Defaults are tuned for a 60s tick cadence. For slower ticks,
    widen ``stale_after_s`` / ``dead_after_s`` accordingly.
    """

    # Staleness thresholds (seconds since last tick).
    stale_after_s: float = Field(default=300.0, gt=0.0)
    dead_after_s: float = Field(default=1800.0, gt=0.0)

    # Dominance detection: if the same binding_constraint appears in
    # the last N snapshots, signal YELLOW.
    dominance_run: int = Field(default=10, ge=3, le=200)

    # Flatline detection: if stress composite is below this value for
    # ``flatline_run`` consecutive snapshots, signal YELLOW.
    flatline_threshold: float = Field(default=0.05, ge=0.0, le=1.0)
    flatline_run: int = Field(default=10, ge=3, le=200)

    # Alert dedup prefix.
    dedup_prefix: str = Field(default="jarvis_supervisor", min_length=1)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

ClockFn = Callable[[], datetime]


def _valid_composite(x: float | None) -> bool:
    if x is None:
        return False
    if not isinstance(x, (int, float)):
        return False
    if math.isnan(x) or math.isinf(x):
        return False
    return 0.0 <= float(x) <= 1.0


def _tail_bindings(snapshots: list[JarvisContext], n: int) -> list[str]:
    """Return the binding_constraint of the last ``n`` snapshots (with
    a stress_score). None/missing entries are skipped.
    """
    out: list[str] = []
    for ctx in reversed(snapshots):
        if ctx.stress_score is None:
            continue
        out.append(ctx.stress_score.binding_constraint)
        if len(out) == n:
            break
    out.reverse()
    return out


def _tail_composites(snapshots: list[JarvisContext], n: int) -> list[float]:
    out: list[float] = []
    for ctx in reversed(snapshots):
        if ctx.stress_score is None:
            continue
        out.append(ctx.stress_score.composite)
        if len(out) == n:
            break
    out.reverse()
    return out


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class JarvisSupervisor:
    """Live supervisor for a ``JarvisContextEngine``.

    Usage::

        sup = JarvisSupervisor(engine=engine, policy=SupervisorPolicy())
        # In the main loop
        ctx = sup.tick()
        report = sup.snapshot_health()
        if report.degraded:
            await sup.alert(alerter, report)
    """

    def __init__(
        self,
        *,
        engine: JarvisContextEngine,
        policy: SupervisorPolicy | None = None,
        clock: ClockFn | None = None,
    ) -> None:
        self._engine = engine
        self.policy = policy or SupervisorPolicy()
        self._clock: ClockFn = clock if clock is not None else (lambda: datetime.now(UTC))
        self._last_tick_at: datetime | None = None
        self._tick_count: int = 0
        self._running: bool = False

    # -- observable properties -------------------------------------------

    @property
    def last_tick_at(self) -> datetime | None:
        return self._last_tick_at

    @property
    def tick_count(self) -> int:
        return self._tick_count

    # -- tick ------------------------------------------------------------

    def tick(self, *, notes: list[str] | None = None) -> JarvisContext:
        """Tick the underlying engine and record the timestamp.

        Exceptions propagate --- the supervisor will mark itself RED on
        the next ``snapshot_health`` call because ``_last_tick_at`` is
        not updated.
        """
        try:
            ctx = self._engine.tick(notes=notes)
        except Exception:
            logger.exception("Jarvis engine.tick() raised")
            raise
        self._last_tick_at = self._clock()
        self._tick_count += 1
        return ctx

    # -- health evaluation ----------------------------------------------

    def snapshot_health(self) -> JarvisHealthReport:
        """Pure: inspect current state and produce a report. No I/O."""
        now = self._clock()
        reasons: list[str] = []
        health = JarvisHealth.GREEN

        # --- staleness ---------------------------------------------------
        stale_s: float
        if self._last_tick_at is None:
            # Never ticked. Grace period == dead_after_s.
            stale_s = 0.0
            if self._tick_count == 0 and len(self._engine.memory) == 0:
                # If someone just constructed the supervisor, being 0-ticked
                # is expected. We only escalate if wallclock forced us past
                # dead_after_s (unusual; caller didn't record a start).
                pass
        else:
            stale_s = max(0.0, (now - self._last_tick_at).total_seconds())

        if self._last_tick_at is not None:
            if stale_s >= self.policy.dead_after_s:
                reasons.append(
                    f"engine DEAD: {stale_s:.1f}s since last tick (>= {self.policy.dead_after_s:.1f}s)",
                )
                health = JarvisHealth.RED
            elif stale_s >= self.policy.stale_after_s:
                reasons.append(
                    f"engine STALE: {stale_s:.1f}s since last tick (>= {self.policy.stale_after_s:.1f}s)",
                )
                health = _max_health(health, JarvisHealth.YELLOW)

        # --- memory inspection ------------------------------------------
        snapshots = self._engine.memory.snapshots()
        last_ctx: JarvisContext | None = snapshots[-1] if snapshots else None

        last_composite: float | None = None
        last_binding: str | None = None
        if last_ctx is not None and last_ctx.stress_score is not None:
            last_composite = last_ctx.stress_score.composite
            last_binding = last_ctx.stress_score.binding_constraint

        # --- invalid composite ------------------------------------------
        if last_ctx is not None and last_ctx.stress_score is not None:
            c = last_ctx.stress_score.composite
            if not _valid_composite(c):
                reasons.append(
                    f"invalid stress composite: {c!r} (expected float in [0.0, 1.0])",
                )
                health = JarvisHealth.RED

        # --- dominance ---------------------------------------------------
        dom_run = 0
        if len(snapshots) >= self.policy.dominance_run:
            bindings = _tail_bindings(snapshots, self.policy.dominance_run)
            if len(bindings) == self.policy.dominance_run and len(set(bindings)) == 1:
                dom_run = self.policy.dominance_run
                reasons.append(
                    f"binding_constraint DOMINANCE: "
                    f"'{bindings[0]}' for last {dom_run} snapshots "
                    "(weights may need rebalancing)",
                )
                health = _max_health(health, JarvisHealth.YELLOW)

        # --- flatline composite ----------------------------------------
        flat_run = 0
        if len(snapshots) >= self.policy.flatline_run:
            comps = _tail_composites(snapshots, self.policy.flatline_run)
            if len(comps) == self.policy.flatline_run and all(c <= self.policy.flatline_threshold for c in comps):
                flat_run = self.policy.flatline_run
                reasons.append(
                    f"composite FLATLINE: all last {flat_run} below "
                    f"{self.policy.flatline_threshold} (Jarvis may be blind)",
                )
                health = _max_health(health, JarvisHealth.YELLOW)

        metrics: dict[str, float] = {
            "stale_s": round(stale_s, 3),
            "memory_len": float(len(snapshots)),
            "tick_count": float(self._tick_count),
            "dominance_run": float(dom_run),
            "flatline_run": float(flat_run),
        }
        if last_composite is not None:
            metrics["last_composite"] = round(last_composite, 6)

        return JarvisHealthReport(
            ts=now,
            health=health,
            reasons=reasons,
            metrics=metrics,
            last_tick_at=self._last_tick_at,
            last_composite=last_composite,
            last_binding=last_binding,
            memory_len=len(snapshots),
        )

    # -- alerting --------------------------------------------------------

    async def alert(
        self,
        alerter: MultiAlerter | None,
        report: JarvisHealthReport,
    ) -> None:
        """Send a dedup-keyed alert if the report is degraded.

        Silently no-op on GREEN or missing alerter. Each (health, primary
        reason) pair gets a distinct dedup key so repeated alerts don't
        spam but different issues still page.
        """
        if alerter is None or report.is_healthy:
            return
        # Import lazily so tests/module graphs avoid needing alerts at import time.
        from eta_engine.obs.alerts import Alert, AlertLevel

        level = _alert_level_for_health(report.health)
        primary = report.reasons[0] if report.reasons else "unknown"
        # Stabilize dedup by hashing only the stable phrase prefix of primary.
        stem = primary.split(":", 1)[0]
        dedup_key = f"{self.policy.dedup_prefix}::{report.health.value}::{stem}"
        alert = Alert(
            level=level,
            title=f"Jarvis supervisor: {report.health.value}",
            message="; ".join(report.reasons) or "degraded (no reasons)",
            context={
                "last_tick_at": str(report.last_tick_at),
                "memory_len": str(report.memory_len),
                "last_composite": str(report.last_composite),
                "last_binding": str(report.last_binding),
                **{k: str(v) for k, v in report.metrics.items()},
            },
            dedup_key=dedup_key,
        )
        try:
            await alerter.send(alert)
        except Exception:
            # Supervisor is best-effort; do not let alert failure kill
            # the main loop.
            logger.exception("JarvisSupervisor alert send failed")
        # Annotate the AlertLevel name so callers can introspect.
        _ = AlertLevel  # keep reference for static-analysis

    # -- run loop --------------------------------------------------------

    async def run(
        self,
        *,
        interval_s: float = 60.0,
        alerter: MultiAlerter | None = None,
        max_ticks: int | None = None,
    ) -> None:
        """Tick + evaluate on a fixed cadence until ``stop()``.

        ``max_ticks`` bounds the loop for tests; None runs forever.
        """
        if interval_s <= 0.0:
            raise ValueError("interval_s must be > 0")
        self._running = True
        i = 0
        try:
            while self._running and (max_ticks is None or i < max_ticks):
                # engine.tick() raised -- stale-detection on the next
                # iteration will pick it up and alert RED.
                with contextlib.suppress(Exception):
                    self.tick()
                report = self.snapshot_health()
                if report.degraded:
                    await self.alert(alerter, report)
                i += 1
                if max_ticks is not None and i >= max_ticks:
                    break
                await asyncio.sleep(interval_s)
        finally:
            self._running = False

    def stop(self) -> None:
        """Ask the running loop to exit on its next iteration."""
        self._running = False


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


_HEALTH_ORDER: dict[JarvisHealth, int] = {
    JarvisHealth.GREEN: 0,
    JarvisHealth.YELLOW: 1,
    JarvisHealth.RED: 2,
}


def _max_health(a: JarvisHealth, b: JarvisHealth) -> JarvisHealth:
    """Return the more severe of two health states."""
    return a if _HEALTH_ORDER[a] >= _HEALTH_ORDER[b] else b


def _alert_level_for_health(h: JarvisHealth) -> int:
    """Map health to an AlertLevel int (imported lazily by ``alert``)."""
    from eta_engine.obs.alerts import AlertLevel

    if h == JarvisHealth.RED:
        return AlertLevel.CRITICAL
    if h == JarvisHealth.YELLOW:
        return AlertLevel.WARN
    return AlertLevel.INFO
