"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.hardened_fleet
================================================
Opt-in middleware wrapper that composes every hardening module around
the base ``Fleet``. Callers with no opinion keep using ``Fleet`` directly;
callers who want the full stack swap ``Fleet`` for ``HardenedFleet`` and
get all the guard bands for free.

Why this exists
---------------
We built seven independent guard modules (precedent cache, dead-man
switch, circuit breaker, calibration loop, cost forecast, regime gate,
watchdog). Wiring each one into every dispatch by hand is error-prone --
it is exactly the kind of copy-paste that a lazy Tuesday reformats away.
This module is the one place that knows the canonical order.

Order of operations (per dispatch)
----------------------------------
1. **Circuit breaker pre-check** -- if OPEN, short-circuit with a
   ``reason_code='breaker_open'`` TaskResult. Never invokes the persona.
2. **Dead-man gate** -- if the operator has gone silent and the envelope
   is in the stale-blocked set, short-circuit with
   ``reason_code='deadman_blocked'``.
3. **Precedent cache** -- if K prior successes with high similarity exist,
   synthesize a ``reason_code='precedent_reuse'`` TaskResult reusing the
   freshest artifact.
4. **Fleet.dispatch** -- real work.
5. **Calibration record** -- update the persona/category scoreboard.
6. **Circuit breaker record** -- feed the result back for trip detection.
7. **Push on anomaly** -- fire a Pushover / Telegram alert when the
   breaker actually trips or the deadman fires for the first time.

Everything is off by default. Pass ``None`` for any component to disable
that guard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from eta_engine.brain.avengers.base import (
    COST_RATIO,
    PersonaId,
    TaskResult,
)
from eta_engine.brain.avengers.circuit_breaker import BreakerTripped
from eta_engine.brain.avengers.push import AlertLevel, default_bus

if TYPE_CHECKING:
    from eta_engine.brain.avengers.base import TaskEnvelope
    from eta_engine.brain.avengers.calibration_loop import CalibrationLoop
    from eta_engine.brain.avengers.circuit_breaker import CircuitBreaker
    from eta_engine.brain.avengers.deadman import DeadmanSwitch
    from eta_engine.brain.avengers.fleet import Fleet, FleetMetrics
    from eta_engine.brain.avengers.precedent_cache import PrecedentCache
    from eta_engine.brain.avengers.push import PushBus


class HardenedFleet:
    """Wraps a ``Fleet`` with the composable guard stack.

    Parameters
    ----------
    fleet
        The underlying ``Fleet``. All routing / executor wiring stays
        there; HardenedFleet only decorates.
    precedent_cache
        If present, consulted BEFORE dispatch. Hits short-circuit.
    deadman
        If present, ``decide(envelope)`` is called BEFORE dispatch. A
        ``allow=False`` short-circuits with ``deadman_blocked``.
    breaker
        If present, ``pre_dispatch()`` is called BEFORE dispatch; if it
        raises, we synthesize a ``breaker_open`` result. After dispatch,
        ``record(result)`` is called.
    calibration
        If present, ``record(result)`` is called after every dispatch.
    push_bus
        If present, receives alerts on breaker-trip / deadman-freeze.
        Defaults to ``default_bus()``.
    """

    def __init__(
        self,
        fleet: Fleet,
        *,
        precedent_cache: PrecedentCache | None = None,
        deadman: DeadmanSwitch | None = None,
        breaker: CircuitBreaker | None = None,
        calibration: CalibrationLoop | None = None,
        push_bus: PushBus | None = None,
    ) -> None:
        self.fleet = fleet
        self.precedent_cache = precedent_cache
        self.deadman = deadman
        self.breaker = breaker
        self.calibration = calibration
        self.push_bus = push_bus or default_bus()
        self._last_breaker_state: str | None = None
        self._last_deadman_state: str | None = None

    # --- dispatch chain ----------------------------------------------------

    def dispatch(self, envelope: TaskEnvelope) -> TaskResult:
        """Route through the guard stack. Never raises for expected denials."""
        # 1. Circuit breaker pre-check ------------------------------------
        if self.breaker is not None:
            try:
                self.breaker.pre_dispatch()
            except BreakerTripped as exc:
                result = self._synthesize(
                    envelope=envelope,
                    reason_code="breaker_open",
                    reason=str(exc),
                )
                self._maybe_alert_breaker()
                return result

        # 2. Dead-man gate ------------------------------------------------
        if self.deadman is not None:
            decision = self.deadman.decide(envelope)
            if not decision.allow:
                result = self._synthesize(
                    envelope=envelope,
                    reason_code="deadman_blocked",
                    reason=decision.reason,
                )
                self._maybe_alert_deadman(decision.state.value)
                # Feed synthetic failure to breaker so repeated stale-mode
                # denials don't build false consecutive-failure counts.
                return result

        # 3. Precedent cache ---------------------------------------------
        if self.precedent_cache is not None:
            skip = self.precedent_cache.should_skip(envelope)
            if skip is not None:
                result = self._synthesize(
                    envelope=envelope,
                    reason_code="precedent_reuse",
                    reason=skip.reason,
                    artifact=skip.reused_artifact,
                    success=True,
                    cost_multiplier=0.0,
                )
                if self.calibration is not None:
                    self.calibration.record(envelope, result)
                return result

        # 4. Real dispatch ------------------------------------------------
        result = self.fleet.dispatch(envelope)

        # 5. Calibration feedback ----------------------------------------
        if self.calibration is not None:
            self.calibration.record(envelope, result)

        # 6. Breaker feedback --------------------------------------------
        if self.breaker is not None:
            self.breaker.record(result)

        return result

    # --- synthetic result builder ------------------------------------------

    def _synthesize(
        self,
        *,
        envelope: TaskEnvelope,
        reason_code: str,
        reason: str,
        artifact: str = "",
        success: bool = False,
        cost_multiplier: float = 0.0,
    ) -> TaskResult:
        """Build a TaskResult without actually invoking a persona."""
        # Pick the persona that WOULD have answered, for attribution.
        try:
            pid = self.fleet._pick_persona(envelope)  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pid = PersonaId.ALFRED
        return TaskResult(
            task_id=envelope.task_id,
            persona_id=pid,
            tier_used=None,
            success=success,
            artifact=artifact,
            reason_code=reason_code,
            reason=reason,
            cost_multiplier=cost_multiplier,
            jarvis_verdict=None,
            ms_elapsed=0.0,
            ts=datetime.now(UTC),
        )

    # --- alerts ------------------------------------------------------------

    def _maybe_alert_breaker(self) -> None:
        if self.breaker is None:
            return
        status = self.breaker.status()
        state = status.state.value
        if state == self._last_breaker_state:
            return
        self._last_breaker_state = state
        if state != "OPEN":
            return
        try:
            self.push_bus.push(
                level=AlertLevel.CRITICAL,
                title="JARVIS circuit breaker tripped",
                body=(
                    f"state=OPEN reason={status.last_reason}\n"
                    f"consec_failures={status.consec_failures} "
                    f"consec_denials={status.consec_denials} "
                    f"cost/min={status.cost_window_sum:.2f}"
                ),
                source="hardened_fleet",
                tags=["breaker", "open"],
            )
        except Exception:
            return

    def _maybe_alert_deadman(self, state: str) -> None:
        if state == self._last_deadman_state:
            return
        self._last_deadman_state = state
        if state not in {"STALE", "FROZEN"}:
            return
        try:
            self.push_bus.push(
                level=AlertLevel.WARN if state == "STALE" else AlertLevel.CRITICAL,
                title=f"JARVIS dead-man switch: {state}",
                body=(
                    f"operator hasn't touched JARVIS long enough to enter "
                    f"{state} mode. Spend-money dispatches are now gated."
                ),
                source="hardened_fleet",
                tags=["deadman", state.lower()],
            )
        except Exception:
            return

    # --- passthrough helpers ----------------------------------------------

    def pool(self, *args, **kwargs) -> list[TaskResult]:  # noqa: ANN002, ANN003
        """Delegate to Fleet.pool -- pool bypasses the guard stack."""
        return self.fleet.pool(*args, **kwargs)

    def metrics(self) -> FleetMetrics:
        return self.fleet.metrics()

    def describe(self) -> list[str]:
        lines = list(self.fleet.describe())
        if self.breaker is not None:
            lines.append(f"breaker: state={self.breaker.status().state.value}")
        if self.deadman is not None:
            st = self.deadman.status()
            lines.append(
                f"deadman: state={st.state.value} hours_since={st.hours_since:.1f}",
            )
        if self.calibration is not None:
            snap = self.calibration.snapshot()
            lines.append(f"calibration: {len(snap)} (persona,category) buckets")
        if self.precedent_cache is not None:
            lines.append(
                f"precedent_cache: lookback={self.precedent_cache.lookback_days}d "
                f"min_sim={self.precedent_cache.min_similarity}",
            )
        return lines


__all__ = [
    "COST_RATIO",
    "HardenedFleet",
]
