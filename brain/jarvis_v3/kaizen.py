"""
JARVIS v3 // kaizen
===================
Continuous-improvement loop.

Kaizen is a first-class tenet of the Evolutionary Trading Algo doctrine. Every cycle
ends with a retrospective that produces at least one concrete +1 --
one shippable improvement, however small.

This module mechanizes that:

  * ``Retrospective``     -- structured record of one cycle's review
  * ``KaizenTicket``      -- the +1 action item produced
  * ``KaizenLedger``      -- append-only JSONL log of retros + tickets
  * ``close_cycle``       -- ingest new evidence + produce next +1
  * ``kaizen_health``     -- how often retros fire; any missed cycles?

A "cycle" is operator-defined. Typical: one trading day. But the ledger
supports ad-hoc cycles (post-incident, post-strategy-promotion, etc.)

Pure stdlib + pydantic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class CycleKind(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    POST_INCIDENT = "POST_INCIDENT"
    POST_DEPLOY = "POST_DEPLOY"
    AD_HOC = "AD_HOC"


class Retrospective(BaseModel):
    """One closed retrospective."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    cycle_kind: CycleKind
    window_start: datetime
    window_end: datetime
    went_well: list[str] = Field(default_factory=list)
    went_poorly: list[str] = Field(default_factory=list)
    surprises: list[str] = Field(default_factory=list)
    kpis: dict[str, float] = Field(default_factory=dict)
    lessons: list[str] = Field(default_factory=list)


class KaizenStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    SHIPPED = "SHIPPED"
    DROPPED = "DROPPED"


class KaizenTicket(BaseModel):
    """One +1 item emitted by a retrospective."""

    model_config = ConfigDict(frozen=False)

    id: str = Field(min_length=1)
    parent_retrospective_ts: datetime
    title: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    status: KaizenStatus = KaizenStatus.OPEN
    impact: str = Field(default="medium", pattern="^(small|medium|large|critical)$")
    owner: str = "operator.edward"
    opened_at: datetime
    shipped_at: datetime | None = None
    drop_reason: str = ""


class KaizenCycleSummary(BaseModel):
    """Roll-up view: how we're doing."""

    model_config = ConfigDict(frozen=True)

    window_days: int
    retrospectives: int
    tickets_opened: int
    tickets_shipped: int
    tickets_dropped: int
    velocity: float = Field(description="shipped tickets per retrospective")
    missed_cycles: int
    severity: str = Field(pattern="^(GREEN|YELLOW|RED)$")
    note: str


class KaizenLedger:
    """Append-only ledger of retros + tickets."""

    def __init__(self) -> None:
        self._retros: list[Retrospective] = []
        self._tickets: dict[str, KaizenTicket] = {}

    def add_retro(self, r: Retrospective) -> None:
        self._retros.append(r)

    def add_ticket(self, t: KaizenTicket) -> None:
        self._tickets[t.id] = t

    def ship_ticket(self, ticket_id: str, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if ticket_id in self._tickets:
            t = self._tickets[ticket_id]
            t.status = KaizenStatus.SHIPPED
            t.shipped_at = now

    def drop_ticket(self, ticket_id: str, reason: str) -> None:
        if ticket_id in self._tickets:
            t = self._tickets[ticket_id]
            t.status = KaizenStatus.DROPPED
            t.drop_reason = reason

    def retrospectives(self) -> list[Retrospective]:
        return list(self._retros)

    def tickets(self) -> list[KaizenTicket]:
        return list(self._tickets.values())

    def summary(
        self,
        window_days: int = 7,
        now: datetime | None = None,
    ) -> KaizenCycleSummary:
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=window_days)
        retros = [r for r in self._retros if r.ts >= cutoff]
        tix = [t for t in self._tickets.values() if t.opened_at >= cutoff]
        shipped = [t for t in tix if t.status == KaizenStatus.SHIPPED]
        dropped = [t for t in tix if t.status == KaizenStatus.DROPPED]
        velocity = len(shipped) / max(1, len(retros))
        # Missed cycles: expected at least one retro per day
        expected = window_days if window_days > 0 else 1
        missed = max(0, expected - len(retros))
        if missed >= 3:
            severity = "RED"
            note = f"{missed} missed retrospectives in {window_days}d -- KAIZEN breach"
        elif missed > 0:
            severity = "YELLOW"
            note = f"{missed} missed retrospective in {window_days}d"
        else:
            severity = "GREEN"
            note = f"retrospective cadence honored ({len(retros)} in {window_days}d)"
        return KaizenCycleSummary(
            window_days=window_days,
            retrospectives=len(retros),
            tickets_opened=len(tix),
            tickets_shipped=len(shipped),
            tickets_dropped=len(dropped),
            velocity=round(velocity, 4),
            missed_cycles=missed,
            severity=severity,
            note=note,
        )

    # Persistence -------------------------------------------------------
    def save(self, path: Path | str) -> None:
        data = {
            "retrospectives": [r.model_dump(mode="json") for r in self._retros],
            "tickets": [t.model_dump(mode="json") for t in self._tickets.values()],
        }
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> KaizenLedger:
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        ledger = cls()
        for r in data.get("retrospectives", []):
            ledger._retros.append(Retrospective.model_validate(r))
        for t in data.get("tickets", []):
            tk = KaizenTicket.model_validate(t)
            ledger._tickets[tk.id] = tk
        return ledger


def close_cycle(
    *,
    cycle_kind: CycleKind,
    window_start: datetime,
    window_end: datetime,
    went_well: list[str],
    went_poorly: list[str],
    surprises: list[str] | None = None,
    kpis: dict[str, float] | None = None,
    now: datetime | None = None,
) -> tuple[Retrospective, KaizenTicket]:
    """Close a cycle -- produce a retrospective plus a guaranteed +1 ticket.

    The +1 ticket priority is derived from the first ``went_poorly`` entry,
    or -- if everything went well -- the first ``surprises`` entry, or a
    generic "review observability gaps" placeholder. Doctrine requires
    that EVERY cycle emit at least one ticket (Kaizen = +1 always).
    """
    now = now or datetime.now(UTC)
    lessons: list[str] = []
    if went_poorly:
        lessons.append(f"address: {went_poorly[0]}")
    elif surprises:
        lessons.append(f"investigate surprise: {surprises[0]}")

    retro = Retrospective(
        ts=now,
        cycle_kind=cycle_kind,
        window_start=window_start,
        window_end=window_end,
        went_well=went_well,
        went_poorly=went_poorly,
        surprises=surprises or [],
        kpis=kpis or {},
        lessons=lessons,
    )

    # Emit the +1
    title, rationale, impact = _select_plus_one(retro)
    tid = _ticket_id(now, title)
    ticket = KaizenTicket(
        id=tid,
        parent_retrospective_ts=now,
        title=title,
        rationale=rationale,
        impact=impact,
        opened_at=now,
    )
    return retro, ticket


def _select_plus_one(r: Retrospective) -> tuple[str, str, str]:
    if r.went_poorly:
        return (
            f"Fix: {r.went_poorly[0][:60]}",
            f"Retrospective flagged this as going poorly in the "
            f"{r.cycle_kind.value} cycle ending {r.window_end.isoformat()}",
            "large",
        )
    if r.surprises:
        return (
            f"Investigate: {r.surprises[0][:60]}",
            f"Unexpected event in {r.cycle_kind.value} cycle; must reduce surprise surface area",
            "medium",
        )
    # Baseline Kaizen +1 when nothing is broken.
    return (
        "Tighten observability",
        "Default +1 when nothing is broken: raise telemetry bar (log "
        "coverage, alert resolution time, dashboard freshness)",
        "small",
    )


def _ticket_id(ts: datetime, title: str) -> str:
    prefix = ts.strftime("%Y%m%d-%H%M")
    slug = "".join(c.lower() if c.isalnum() else "-" for c in title)[:40].strip("-")
    return f"KZN-{prefix}-{slug}"


def kaizen_score(summary: KaizenCycleSummary) -> float:
    """Numeric 0..1 score for use in doctrine bias calculations."""
    if summary.severity == "RED":
        return 0.0
    if summary.severity == "YELLOW":
        return 0.5
    # GREEN: scale by velocity (shipped/retro)
    return min(1.0, 0.6 + 0.4 * min(1.0, summary.velocity))
