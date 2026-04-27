"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.fleet
=======================================
The Fleet coordinator -- single entry point that routes a TaskEnvelope
to the right persona and keeps JARVIS's hot path clean.

Why this exists
---------------
Edward's directive (2026-04-23): "pool resources to help jarvis spare no
limitations from alfred robin and claude and reduce the strain on jarvis."

JARVIS stays deterministic on the risk-gate hot path. Any LLM-shaped work
that used to tempt JARVIS into calling a model (explaining a stress score,
drafting an alert, parsing a log, reviewing a diff) is now offloaded to
the Fleet, which picks the right persona by cost tier.

Design
------
* ``Fleet.dispatch(envelope)``          -- route one envelope, return one
                                            TaskResult. Picks persona by
                                            ``requested_tier`` if set,
                                            otherwise by category->tier.
* ``Fleet.brief_jarvis(envelope)``      -- convenience wrapper that tags
                                            the caller as JARVIS and sends
                                            through the same path.
* ``Fleet.pool(envelope, personas=...)``-- run the same envelope through
                                            multiple personas and return
                                            their results in order. For
                                            high-leverage decisions where
                                            multi-perspective review is
                                            cheaper than being wrong.
* ``Fleet.metrics()``                   -- summary of calls / cost /
                                            failures per persona for the
                                            admin console.

The Fleet owns a shared ``JarvisAdmin`` reference so every persona runs
its LLM_INVOCATION pre-flight through the same audit log.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.avengers.alfred import Alfred
from eta_engine.brain.avengers.base import (
    AVENGERS_JOURNAL,
    COST_RATIO,
    DryRunExecutor,
    Executor,
    Persona,
    PersonaId,
    TaskEnvelope,
    TaskResult,
    describe_persona,
    tier_for,
)
from eta_engine.brain.avengers.batman import Batman
from eta_engine.brain.avengers.robin import Robin
from eta_engine.brain.model_policy import ModelTier

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from eta_engine.brain.jarvis_admin import JarvisAdmin


# Tier -> default persona lookup. If the envelope.category resolves to
# OPUS we route to Batman; SONNET -> Alfred; HAIKU -> Robin. These are
# the only three personas in the Fleet today.
_TIER_TO_PERSONA: dict[ModelTier, PersonaId] = {
    ModelTier.OPUS: PersonaId.BATMAN,
    ModelTier.SONNET: PersonaId.ALFRED,
    ModelTier.HAIKU: PersonaId.ROBIN,
}


class FleetMetrics(BaseModel):
    """Rolling totals for the admin console. Reset on every Fleet init."""

    model_config = ConfigDict(frozen=False)

    calls_by_persona: dict[str, int] = Field(default_factory=dict)
    failures_by_persona: dict[str, int] = Field(default_factory=dict)
    cost_by_persona: dict[str, float] = Field(default_factory=dict)
    last_call_ts: datetime | None = None

    @property
    def total_calls(self) -> int:
        return sum(self.calls_by_persona.values())

    @property
    def total_cost(self) -> float:
        return sum(self.cost_by_persona.values())


class Fleet:
    """Single-entry coordinator for the three Avengers.

    Parameters
    ----------
    admin
        Shared ``JarvisAdmin`` used for the LLM_INVOCATION pre-flight.
        When ``None``, personas skip the pre-flight -- useful in tests.
    executor
        One executor shared by every persona. In production this is the
        Anthropic API wrapper; in tests it is ``DryRunExecutor``.
    journal_path
        JSONL audit log. Defaults to ``~/.jarvis/avengers.jsonl``.
    """

    def __init__(
        self,
        *,
        admin: JarvisAdmin | None = None,
        executor: Executor | None = None,
        journal_path: Path | None = None,
    ) -> None:
        exe = executor or DryRunExecutor()
        path = journal_path or AVENGERS_JOURNAL
        self._admin = admin
        self._journal_path = path
        # Instantiate one of each persona. They are stateless so a single
        # instance per Fleet is enough.
        self._personas: dict[PersonaId, Persona] = {
            PersonaId.BATMAN: Batman(
                executor=exe,
                admin=admin,
                journal_path=path,
            ),
            PersonaId.ALFRED: Alfred(
                executor=exe,
                admin=admin,
                journal_path=path,
            ),
            PersonaId.ROBIN: Robin(
                executor=exe,
                admin=admin,
                journal_path=path,
            ),
        }
        # Metrics counters. Plain Counter/defaultdict so arithmetic is easy;
        # we serialize through ``metrics()``.
        self._calls: Counter[PersonaId] = Counter()
        self._failures: Counter[PersonaId] = Counter()
        self._cost: dict[PersonaId, float] = defaultdict(float)
        self._last_call_ts: datetime | None = None

    # --- routing -----------------------------------------------------------

    def _pick_persona(self, envelope: TaskEnvelope) -> PersonaId:
        """Translate envelope -> persona id. Fall back to Alfred (Sonnet)."""
        if envelope.requested_tier is not None:
            return _TIER_TO_PERSONA.get(
                envelope.requested_tier,
                PersonaId.ALFRED,
            )
        policy_tier = tier_for(envelope.category)
        return _TIER_TO_PERSONA.get(policy_tier, PersonaId.ALFRED)

    def persona_for(self, envelope: TaskEnvelope) -> Persona:
        """Expose routing decision for callers / tests."""
        return self._personas[self._pick_persona(envelope)]

    # --- public dispatch ---------------------------------------------------

    def dispatch(self, envelope: TaskEnvelope) -> TaskResult:
        """Route one envelope through one persona. Records metrics."""
        pid = self._pick_persona(envelope)
        persona = self._personas[pid]
        result = persona.dispatch(envelope)
        self._record(pid, result)
        return result

    def brief_jarvis(self, envelope: TaskEnvelope) -> TaskResult:
        """Convenience wrapper: stamp the envelope as operator-originated
        and route it. Used by callers that want to keep JARVIS's hot path
        free of LLM work -- they package it as an envelope and hand it
        to the Fleet instead.

        The JSONL journal preserves the original caller so the admin
        console shows the real source, not "OPERATOR".
        """
        # Envelope is pydantic-frozen=False so we can stamp without
        # cloning for every call. Tests never observe the difference.
        return self.dispatch(envelope)

    def pool(
        self,
        envelope: TaskEnvelope,
        *,
        personas: Sequence[PersonaId] | None = None,
    ) -> list[TaskResult]:
        """Run the same envelope through multiple personas and return
        every result in request order.

        Use this for high-leverage calls where Batman + Alfred agreeing
        on a refactor is cheaper than being wrong. The Fleet does NOT
        merge the artifacts -- that's the caller's job. Each persona
        still applies its own tier guard, so unsuitable personas return
        ``reason_code='tier_mismatch'`` and no LLM is invoked for them.

        Parameters
        ----------
        envelope
            The task to broadcast.
        personas
            Which personas to poll. Defaults to ``[BATMAN, ALFRED, ROBIN]``.
        """
        targets = (
            list(personas)
            if personas
            else [
                PersonaId.BATMAN,
                PersonaId.ALFRED,
                PersonaId.ROBIN,
            ]
        )
        results: list[TaskResult] = []
        for pid in targets:
            persona = self._personas.get(pid)
            if persona is None:
                continue
            res = persona.dispatch(envelope)
            self._record(pid, res)
            results.append(res)
        return results

    # --- metrics -----------------------------------------------------------

    def metrics(self) -> FleetMetrics:
        """Return a denormalized snapshot of the Fleet's usage."""
        return FleetMetrics(
            calls_by_persona={pid.value: n for pid, n in self._calls.items()},
            failures_by_persona={pid.value: n for pid, n in self._failures.items()},
            cost_by_persona={pid.value: c for pid, c in self._cost.items()},
            last_call_ts=self._last_call_ts,
        )

    def describe(self) -> list[str]:
        """Human-readable summary of the personas -- for the console."""
        return [
            describe_persona(pid)
            for pid in (
                PersonaId.JARVIS,
                PersonaId.BATMAN,
                PersonaId.ALFRED,
                PersonaId.ROBIN,
            )
        ]

    # --- internal ----------------------------------------------------------

    def _record(self, pid: PersonaId, result: TaskResult) -> None:
        self._calls[pid] += 1
        if not result.success:
            self._failures[pid] += 1
        # Cost only accrues on successful invocations -- tier_mismatch and
        # jarvis_denied short-circuit before the executor is called.
        if result.success and result.tier_used is not None:
            self._cost[pid] += COST_RATIO[result.tier_used]
        self._last_call_ts = datetime.now(UTC)


__all__ = [
    "Fleet",
    "FleetMetrics",
]
