"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.deadman
=========================================
Operator dead-man's switch. If the human hasn't interacted with JARVIS for
a while, flip the system into conservative mode until they resume.

Why this exists
---------------
Two failure modes we want to rule out:

1. **Operator is AWOL** (sleeping, travelling, sick, cognitively busy).
   The daemons keep firing. A misconfigured Opus bill or a runaway live
   loop can compound for hours before anyone notices. We want a silent,
   automatic "slow down" that kicks in without operator action.
2. **Operator is dead / locked out / VPS hijacked**. In the extreme case
   we want the system to freeze all spend-money actions until a human
   comes back with a fresh sentinel touch.

This is NOT a kill switch (that's ``brain.risk.kill_switch``). This is a
soft governor layered on top.

Design
------
Single sentinel file: ``~/.jarvis/operator.sentinel``. Its mtime is
"last operator activity". Every CLI command in ``scripts/jarvis_cli.py``
calls :py:meth:`DeadmanSwitch.record_activity` on startup; daemons do
NOT touch it (they're not the operator).

Three thresholds:

=================  ======  =====================================================
State              Hours   What changes
=================  ======  =====================================================
``LIVE``           0-12    Nothing. Operator is active.
``DROWSY``         12-24   Warn in reason field. No gating yet.
``STALE``          24-72   Conservative: block expensive categories (OPUS-tier
                           LLM_INVOCATION, promotions, live scale-ups).
``FROZEN``         72+     Freeze: deny anything that costs money. Only
                           safety / diagnostic tasks allowed.
=================  ======  =====================================================

The state machine is monotone until the operator touches the sentinel
(``record_activity``), which snaps back to ``LIVE``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from eta_engine.brain.avengers.base import TaskEnvelope


DEADMAN_SENTINEL: Path = Path.home() / ".jarvis" / "operator.sentinel"
DEADMAN_JOURNAL: Path = Path.home() / ".jarvis" / "operator_activity.jsonl"


# Categories whose cost or blast radius we want to block when STALE.
# These strings match ``TaskCategory`` values (the enum's string values,
# e.g. ``"red_team_scoring"``). We keep the list as strings on purpose
# so a missing enum value in one branch doesn't blow up import.
_STALE_BLOCKED: frozenset[str] = frozenset(
    {
        # Architectural OPUS spend -- most expensive tier.
        "red_team_scoring",
        "gauntlet_gate_design",
        "risk_policy_design",
        "architecture_decision",
        "adversarial_review",
        "state_machine_design",
        # Routine SONNET spend that can touch live strategy code.
        "strategy_edit",
    }
)

# Categories that remain allowed even in FROZEN mode. Everything outside
# this set is denied when frozen. Only safe, cheap, read-only work.
_FROZEN_ALLOWED: frozenset[str] = frozenset(
    {
        "log_parsing",
        "trivial_lookup",
        "formatting",
        "boilerplate",
        "commit_message",
        "simple_edit",
        "lint_fix",
    }
)


class DeadmanState(StrEnum):
    """Current operator-presence state."""

    LIVE = "LIVE"  # fresh operator activity
    DROWSY = "DROWSY"  # stale-ish, still allowed
    STALE = "STALE"  # conservative mode
    FROZEN = "FROZEN"  # deny all spend actions


class DeadmanDecision(BaseModel):
    """One gate decision for a given envelope."""

    model_config = ConfigDict(frozen=True)

    state: DeadmanState
    allow: bool
    reason: str
    hours_since: float = Field(ge=0.0)
    last_activity: datetime | None = None


class DeadmanStatus(BaseModel):
    """Snapshot for dashboards / CLI."""

    model_config = ConfigDict(frozen=True)

    state: DeadmanState
    last_activity: datetime | None
    hours_since: float = Field(ge=0.0)
    soft_stale_hours: float
    hard_stale_hours: float
    freeze_hours: float
    sentinel_path: str


class DeadmanSwitch:
    """File-backed, time-based operator-presence gate.

    Parameters
    ----------
    sentinel_path
        Path to the mtime-sentinel file. Defaults to ``~/.jarvis/operator.sentinel``.
    soft_stale_hours
        After this many hours without activity the state is DROWSY.
    hard_stale_hours
        After this many hours the state is STALE (conservative mode).
    freeze_hours
        After this many hours the state is FROZEN (deny all spend).
    """

    def __init__(
        self,
        *,
        sentinel_path: Path | None = None,
        journal_path: Path | None = None,
        soft_stale_hours: float = 12.0,
        hard_stale_hours: float = 24.0,
        freeze_hours: float = 72.0,
        clock: callable | None = None,
    ) -> None:
        if not (0 < soft_stale_hours < hard_stale_hours < freeze_hours):
            msg = "deadman thresholds must satisfy 0 < soft < hard < freeze"
            raise ValueError(msg)
        self.sentinel_path = sentinel_path or DEADMAN_SENTINEL
        self.journal_path = journal_path or DEADMAN_JOURNAL
        self.soft_stale_hours = soft_stale_hours
        self.hard_stale_hours = hard_stale_hours
        self.freeze_hours = freeze_hours
        self._clock = clock or (lambda: datetime.now(UTC))

    # --- operator API ------------------------------------------------------

    def record_activity(self, source: str = "cli", note: str = "") -> None:
        """Refresh the sentinel. Snaps state back to LIVE.

        Called by every CLI entry point and by the operator-console
        heartbeat. Daemons do NOT call this -- they are not the operator.
        """
        self.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        # Touch sentinel (create if missing, refresh mtime otherwise).
        self.sentinel_path.write_text(self._clock().isoformat(), encoding="utf-8")
        # Append activity log.
        try:
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": self._clock().isoformat(),
                            "source": source,
                            "note": note,
                        }
                    )
                    + "\n"
                )
        except OSError:
            # Activity log is best-effort; the sentinel is the real signal.
            return

    def last_activity(self) -> datetime | None:
        """Return the mtime of the sentinel, or None if it doesn't exist."""
        if not self.sentinel_path.exists():
            return None
        try:
            mtime = self.sentinel_path.stat().st_mtime
        except OSError:
            return None
        return datetime.fromtimestamp(mtime, tz=UTC)

    def hours_since_activity(self) -> float:
        """Hours since last operator touch. ``inf`` if never touched."""
        last = self.last_activity()
        if last is None:
            return float("inf")
        delta = self._clock() - last
        return max(0.0, delta.total_seconds() / 3600.0)

    def state(self) -> DeadmanState:
        hrs = self.hours_since_activity()
        if hrs >= self.freeze_hours:
            return DeadmanState.FROZEN
        if hrs >= self.hard_stale_hours:
            return DeadmanState.STALE
        if hrs >= self.soft_stale_hours:
            return DeadmanState.DROWSY
        return DeadmanState.LIVE

    def is_stale(self) -> bool:
        """Quick bool: are we in STALE or FROZEN?"""
        return self.state() in {DeadmanState.STALE, DeadmanState.FROZEN}

    # --- gate --------------------------------------------------------------

    def decide(self, envelope: TaskEnvelope) -> DeadmanDecision:
        """Gate decision for one envelope. Compose into dispatch pipeline."""
        state = self.state()
        hrs = self.hours_since_activity()
        last = self.last_activity()
        category = envelope.category.value

        if state is DeadmanState.LIVE:
            return DeadmanDecision(
                state=state,
                allow=True,
                reason="operator active",
                hours_since=hrs,
                last_activity=last,
            )

        if state is DeadmanState.DROWSY:
            return DeadmanDecision(
                state=state,
                allow=True,
                reason=(f"operator drowsy ({hrs:.1f}h since last activity); still firing"),
                hours_since=hrs,
                last_activity=last,
            )

        if state is DeadmanState.STALE:
            if category in _STALE_BLOCKED:
                return DeadmanDecision(
                    state=state,
                    allow=False,
                    reason=(f"STALE mode ({hrs:.1f}h since operator); blocking {category}"),
                    hours_since=hrs,
                    last_activity=last,
                )
            return DeadmanDecision(
                state=state,
                allow=True,
                reason=(f"STALE mode ({hrs:.1f}h); {category} still allowed"),
                hours_since=hrs,
                last_activity=last,
            )

        # FROZEN
        if category in _FROZEN_ALLOWED:
            return DeadmanDecision(
                state=state,
                allow=True,
                reason=(f"FROZEN mode ({hrs:.1f}h since operator); {category} on allow-list"),
                hours_since=hrs,
                last_activity=last,
            )
        return DeadmanDecision(
            state=state,
            allow=False,
            reason=(f"FROZEN mode ({hrs:.1f}h since operator); all spend actions denied"),
            hours_since=hrs,
            last_activity=last,
        )

    # --- reporting ---------------------------------------------------------

    def status(self) -> DeadmanStatus:
        return DeadmanStatus(
            state=self.state(),
            last_activity=self.last_activity(),
            hours_since=self.hours_since_activity(),
            soft_stale_hours=self.soft_stale_hours,
            hard_stale_hours=self.hard_stale_hours,
            freeze_hours=self.freeze_hours,
            sentinel_path=str(self.sentinel_path),
        )

    # --- operator overrides ------------------------------------------------

    def resume(self, note: str = "manual resume") -> DeadmanStatus:
        """Explicit 'I'm back' from operator."""
        self.record_activity(source="resume", note=note)
        return self.status()

    def force_stale(self, *, backdate_hours: float | None = None) -> DeadmanStatus:
        """Push the sentinel into the past. For drills / tests."""
        target_hours = backdate_hours
        if target_hours is None:
            target_hours = self.hard_stale_hours + 1.0
        self.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        old_ts = self._clock() - timedelta(hours=target_hours)
        self.sentinel_path.write_text(old_ts.isoformat(), encoding="utf-8")
        try:
            ts = old_ts.timestamp()
            import os

            os.utime(self.sentinel_path, (ts, ts))
        except OSError:
            pass
        return self.status()


__all__ = [
    "DEADMAN_JOURNAL",
    "DEADMAN_SENTINEL",
    "DeadmanDecision",
    "DeadmanState",
    "DeadmanStatus",
    "DeadmanSwitch",
]
