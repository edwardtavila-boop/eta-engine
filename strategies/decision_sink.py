"""EVOLUTIONARY TRADING ALGO  //  strategies.decision_sink.

Bridge :class:`RouterDecision` into the unified
:class:`DecisionJournal` so every live router dispatch is captured
in the same append-only audit log as kill-switch events, firm-board
verdicts, and trade executions.

Design
------
* **Pure conversion helper** -- :func:`router_decision_to_event`
  maps a :class:`RouterDecision` + chosen outcome into a
  :class:`JournalEvent` with ``actor=STRATEGY_ROUTER`` and enough
  metadata to reconstruct the dispatch offline.
* **Thin sink wrapper** -- :class:`RouterDecisionSink` wraps a
  journal plus flags. It's the object a bot's
  :class:`RouterAdapter` holds; calling ``emit(decision)`` writes
  one row or no-ops depending on the gate.
* **No hard dependency from the router package to the obs package**
  -- the helper accepts the already-wrapped decision and a journal
  instance. Bots that don't wire a journal keep running with no
  observability overhead.

Schema
------
Every row looks like::

  {
    "ts": "...",
    "actor": "STRATEGY_ROUTER",
    "intent": "dispatch_{ASSET}",
    "rationale": "{winner.strategy}:{winner.side}",
    "gate_checks": ["+eligible", "+actionable"] or ["-flat"],
    "outcome": "EXECUTED" | "BLOCKED" | "NOTED",
    "links": [optional external refs],
    "metadata": {
      "asset": "...",
      "winner": {...as_dict},
      "candidates_fired": n,
      "eligible": ["liquidity_sweep_displacement", ...],
      "candidates": [...as_dict]      # opt-in only
    }
  }

``outcome`` reflects whether the winner was routed:

* ``EXECUTED`` when the caller reports the trade went out.
* ``BLOCKED`` when kill-switch / risk-gate refused the signal.
* ``NOTED`` (default) when we only want to audit the decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from eta_engine.obs.decision_journal import (
    Actor,
    JournalEvent,
    Outcome,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from eta_engine.obs.decision_journal import DecisionJournal
    from eta_engine.strategies.policy_router import RouterDecision


__all__ = [
    "RouterDecisionSink",
    "router_decision_to_event",
]


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


def router_decision_to_event(
    decision: RouterDecision,
    *,
    outcome: Outcome = Outcome.NOTED,
    links: Sequence[str] | None = None,
    include_candidates: bool = False,
) -> JournalEvent:
    """Convert a :class:`RouterDecision` into a :class:`JournalEvent`.

    Parameters
    ----------
    decision:
        The dispatch output recorded by :class:`RouterAdapter`.
    outcome:
        Sink-caller's classification of what happened downstream.
        Default ``NOTED`` is the safe choice when the caller doesn't
        know whether the bot actually routed the signal.
    links:
        Optional external references (trade_id, order_id, etc.) that
        the sink already has at emit-time.
    include_candidates:
        If True, every candidate's ``as_dict()`` is attached to
        ``metadata["candidates"]``. Heavy; only enable in offline
        research runs or when debugging.
    """
    winner = decision.winner
    is_actionable = winner.is_actionable
    gate_checks: list[str] = []
    if decision.eligible:
        gate_checks.append("+eligible" if decision.eligible else "-eligible")
    if is_actionable:
        gate_checks.append("+actionable")
    else:
        gate_checks.append("-flat")

    metadata: dict[str, object] = {
        "asset": decision.asset,
        "winner": winner.as_dict(),
        "candidates_fired": decision.fired_count,
        "eligible": [e.value for e in decision.eligible],
    }
    if include_candidates:
        metadata["candidates"] = [c.as_dict() for c in decision.candidates]

    return JournalEvent(
        actor=Actor.STRATEGY_ROUTER,
        intent=f"dispatch_{decision.asset}",
        rationale=f"{winner.strategy.value}:{winner.side.value}",
        gate_checks=gate_checks,
        outcome=outcome,
        links=list(links or []),
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Sink
# ---------------------------------------------------------------------------


@dataclass
class RouterDecisionSink:
    """Mutable adapter between :class:`RouterDecision` and a journal.

    Holds the journal reference + emission flags so the bot's
    :class:`RouterAdapter` can emit one row per ``push_bar`` without
    any of the bot code having to know about the observability layer.

    Attributes
    ----------
    journal:
        Target :class:`DecisionJournal`. None disables emission (the
        sink becomes a no-op; useful for tests + log-only runs).
    enabled:
        Runtime toggle. A bot can flip this to silence audit rows
        mid-session (e.g. during a replay dry-run).
    include_candidates:
        Passed straight to :func:`router_decision_to_event`.
    default_outcome:
        What to stamp onto rows when the caller does not override.
    also_log_flat:
        When False (default) the sink only emits rows where the
        winner is actionable -- the common case. Set True to capture
        every dispatch for regime research.
    """

    journal: DecisionJournal | None
    enabled: bool = True
    include_candidates: bool = False
    default_outcome: Outcome = Outcome.NOTED
    also_log_flat: bool = False

    def emit(
        self,
        decision: RouterDecision,
        *,
        outcome: Outcome | None = None,
        links: Sequence[str] | None = None,
    ) -> JournalEvent | None:
        """Write one decision to the journal and return the written event.

        Returns ``None`` when emission is skipped (disabled, no journal,
        or flat-gated). Exceptions are caught so an observability
        failure NEVER crashes the live bot loop; the sink logs to
        stderr and returns None.
        """
        if not self.enabled or self.journal is None:
            return None
        if not self.also_log_flat and not decision.winner.is_actionable:
            return None
        event = router_decision_to_event(
            decision,
            outcome=outcome if outcome is not None else self.default_outcome,
            links=links,
            include_candidates=self.include_candidates,
        )
        try:
            self.journal.append(event)
        except OSError:
            # Journal is append-only plain text; the only realistic
            # failure is a disk/IO issue. Don't let that kill the bot.
            return None
        return event
