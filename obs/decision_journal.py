"""
EVOLUTIONARY TRADING ALGO  //  obs.decision_journal
=======================================
Unified append-only journal for every decision the bot portfolio makes.

Why this exists
---------------
Kill-switch events, firm-board verdicts, trade entries/exits, transfer
approvals, on-ramp pipeline steps, gate overrides -- they all live in
separate logs today (docs/kill_log.json, docs/decisions_v1.json,
TransferLedger, etc.). Pattern mining is impossible if the truth is
fragmented. This module unifies them.

Design
------
* Single newline-delimited JSON file (``docs/decision_journal.jsonl``).
* Append-only: every write is an atomic ``open(mode='a')``.
* Every row is a ``JournalEvent`` with a stable schema + free-form
  ``metadata`` dict for actor-specific fields.
* Readers (``read_all``, ``read_since``, ``read_by_actor``) scan the
  file lazily; no in-memory index by default.
* Pattern mining (``brain/rationale_miner.py``) consumes this file.

Public API
----------
  * ``Actor`` StrEnum (KILL_SWITCH, FIRM_BOARD, TRADE_ENGINE, ...)
  * ``Outcome`` StrEnum (EXECUTED, BLOCKED, OVERRIDDEN, FAILED, NOTED)
  * ``JournalEvent`` pydantic model
  * ``DecisionJournal`` -- append/read wrapper over a Path
  * ``default_journal()`` -- singleton tied to docs/decision_journal.jsonl
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Actor(StrEnum):
    """Who produced this event."""

    KILL_SWITCH = "KILL_SWITCH"
    FIRM_BOARD = "FIRM_BOARD"
    TRADE_ENGINE = "TRADE_ENGINE"
    RISK_GATE = "RISK_GATE"
    TRANSFER_MANAGER = "TRANSFER_MANAGER"
    ONRAMP_PIPELINE = "ONRAMP_PIPELINE"
    OPERATOR = "OPERATOR"
    JARVIS = "JARVIS"
    WATCHDOG = "WATCHDOG"
    GRADER = "GRADER"
    STRATEGY_ROUTER = "STRATEGY_ROUTER"


class Outcome(StrEnum):
    """Result classification for this event."""

    EXECUTED = "EXECUTED"
    BLOCKED = "BLOCKED"
    OVERRIDDEN = "OVERRIDDEN"
    FAILED = "FAILED"
    NOTED = "NOTED"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class JournalEvent(BaseModel):
    """One row in the unified decision journal."""

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: Actor
    intent: str = Field(
        min_length=1,
        description="Human-readable phrase: 'open_mnq_long', 'veto_low_confluence', etc.",
    )
    rationale: str = Field(
        default="",
        description="WHY this action was attempted or taken.",
    )
    gate_checks: list[str] = Field(
        default_factory=list,
        description="Named gates that were evaluated; prefix '+' for pass, '-' for fail",
    )
    outcome: Outcome = Outcome.NOTED
    links: list[str] = Field(
        default_factory=list,
        description="External references: trade_id, order_id, tx_id, spec_id, ...",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Lever 2 (kaizen scaffolding, 2026-04-26): policy version this event
    # was produced under. 0 = pre-policy-versioning (legacy rows). When
    # JARVIS evolves a new policy version through the kaizen+promotion
    # gate, this field lets the replay engine compare v17-vs-v18 behavior
    # over the same event stream.
    policy_version: int = Field(
        default=0,
        ge=0,
        description="JARVIS policy version under which this event was produced.",
    )


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class DecisionJournal:
    """Append-only JSONL decision log.

    One instance per file path. Thread-safe enough for the single-process
    trading stack (append-only writes are atomic on POSIX/NTFS for small
    rows and the bot portfolio is single-process).
    """

    def __init__(self, path: Path | str, *, supabase_mirror: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # When True, every append also fire-and-forget POSTs to Supabase
        # public.decision_journal (silently no-op if env vars unset).
        # Local JSONL stays authoritative regardless of mirror state.
        self._supabase_mirror = supabase_mirror

    # -- write ---------------------------------------------------------------

    def append(self, event: JournalEvent) -> JournalEvent:
        """Write one event. Returns the same event for chaining.

        Local JSONL append is always synchronous and atomic. If
        ``supabase_mirror=True`` (default), the event is also forwarded
        to Supabase ``public.decision_journal`` via the
        ``obs.supabase_sink`` module — best effort, errors swallowed.
        """
        line = event.model_dump_json()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if self._supabase_mirror:
            # Local import keeps the sink module optional and avoids
            # circular references at startup.
            from eta_engine.obs import supabase_sink
            supabase_sink.post_event(event)
        return event

    def record(
        self,
        *,
        actor: Actor,
        intent: str,
        rationale: str = "",
        gate_checks: list[str] | None = None,
        outcome: Outcome = Outcome.NOTED,
        links: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> JournalEvent:
        """Convenience wrapper. Builds + appends a JournalEvent in one call."""
        event = JournalEvent(
            ts=ts if ts is not None else datetime.now(UTC),
            actor=actor,
            intent=intent,
            rationale=rationale,
            gate_checks=gate_checks or [],
            outcome=outcome,
            links=links or [],
            metadata=metadata or {},
        )
        return self.append(event)

    # -- read ----------------------------------------------------------------

    def __len__(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8") as fh:
            return sum(1 for _ in fh)

    def read_all(self) -> list[JournalEvent]:
        """Load every event. For small files only."""
        return list(self._iter_events())

    def iter_all(self) -> Iterator[JournalEvent]:
        """Lazy iterator over events."""
        return self._iter_events()

    def read_since(self, since: datetime) -> list[JournalEvent]:
        return [e for e in self._iter_events() if e.ts >= since]

    def read_by_actor(self, actor: Actor) -> list[JournalEvent]:
        return [e for e in self._iter_events() if e.actor == actor]

    def read_by_outcome(self, outcome: Outcome) -> list[JournalEvent]:
        return [e for e in self._iter_events() if e.outcome == outcome]

    def _iter_events(self) -> Iterator[JournalEvent]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield JournalEvent.model_validate(json.loads(raw))
                except (json.JSONDecodeError, ValueError):
                    # Skip malformed lines rather than crash the consumer.
                    continue

    # -- aggregations --------------------------------------------------------

    def outcome_counts(self) -> dict[Outcome, int]:
        counts: dict[Outcome, int] = dict.fromkeys(Outcome, 0)
        for ev in self._iter_events():
            counts[ev.outcome] = counts.get(ev.outcome, 0) + 1
        return counts

    def actor_counts(self) -> dict[Actor, int]:
        counts: dict[Actor, int] = dict.fromkeys(Actor, 0)
        for ev in self._iter_events():
            counts[ev.actor] = counts.get(ev.actor, 0) + 1
        return counts

    def override_rate(self) -> float:
        """Fraction of RISK_GATE/KILL_SWITCH events that were OVERRIDDEN."""
        gated = [e for e in self._iter_events() if e.actor in (Actor.RISK_GATE, Actor.KILL_SWITCH)]
        if not gated:
            return 0.0
        overridden = sum(1 for e in gated if e.outcome == Outcome.OVERRIDDEN)
        return overridden / len(gated)


# ---------------------------------------------------------------------------
# Default singleton
# ---------------------------------------------------------------------------

_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "docs" / "decision_journal.jsonl"
_default: DecisionJournal | None = None


def default_journal() -> DecisionJournal:
    """Process-wide singleton bound to docs/decision_journal.jsonl."""
    global _default  # noqa: PLW0603
    if _default is None:
        _default = DecisionJournal(_DEFAULT_PATH)
    return _default
