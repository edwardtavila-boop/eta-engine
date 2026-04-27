"""
EVOLUTIONARY TRADING ALGO  //  obs.gate_override_telemetry
==============================================
Single instrumentation entry-point for every gate decision.

Why this exists
---------------
"Never on autopilot" is a principle, not a feature -- the way you enforce
it is to count every override. If you overrode the kill-switch 14 times
last week to make that $400 trade happen, that's the signal. This module:

  * Bumps a Prometheus counter (``apex_gate_overrides_total``).
  * Appends a JournalEvent to DecisionJournal with outcome=OVERRIDDEN.
  * Also tracks BLOCKED events so an override rate can be computed.

Public API
----------
  * ``record_gate_block(gate, reason, actor, metadata=None)``
  * ``record_gate_override(gate, reason, actor, metadata=None)``
  * ``GateTelemetrySummary``  -- cached stats snapshot
  * ``summarize(registry, journal=None)`` -- produce the snapshot
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from eta_engine.obs.metrics import (
    GATE_BLOCKS_TOTAL,
    GATE_OVERRIDE_RATE,
    GATE_OVERRIDES_TOTAL,
    MetricsRegistry,
)

if TYPE_CHECKING:
    from eta_engine.obs.decision_journal import Actor, DecisionJournal


# ---------------------------------------------------------------------------
# Recorders
# ---------------------------------------------------------------------------


def record_gate_block(
    *,
    gate: str,
    reason: str,
    actor: Actor,
    registry: MetricsRegistry,
    journal: DecisionJournal | None = None,
    metadata: dict[str, str] | None = None,
) -> None:
    """A protective gate fired and stopped the action. Good discipline --
    this is what gates exist for.
    """
    if not gate:
        raise ValueError("gate must be non-empty")
    if not reason:
        raise ValueError("reason must be non-empty")
    labels = {"gate": gate, **(metadata or {})}
    registry.inc(GATE_BLOCKS_TOTAL, labels=labels)
    if journal is not None:
        # Import here to avoid circular dep at module load
        from eta_engine.obs.decision_journal import Outcome  # noqa: PLC0415

        journal.record(
            actor=actor,
            intent=f"gate:{gate}:block",
            rationale=reason,
            gate_checks=[f"{gate}:blocked"],
            outcome=Outcome.BLOCKED,
            metadata={"gate": gate, **(metadata or {})},
        )


def record_gate_override(
    *,
    gate: str,
    reason: str,
    actor: Actor,
    registry: MetricsRegistry,
    journal: DecisionJournal | None = None,
    metadata: dict[str, str] | None = None,
) -> None:
    """A gate fired but the operator or an agent overrode it. This is the
    "never on autopilot" signal -- every override is tracked.

    If the override rate climbs, either the gates are miscalibrated OR
    discipline is slipping. Either way the rising counter tells you.
    """
    if not gate:
        raise ValueError("gate must be non-empty")
    if not reason:
        raise ValueError("reason must be non-empty")
    labels = {"gate": gate, **(metadata or {})}
    registry.inc(GATE_OVERRIDES_TOTAL, labels=labels)
    if journal is not None:
        from eta_engine.obs.decision_journal import Outcome  # noqa: PLC0415

        journal.record(
            actor=actor,
            intent=f"gate:{gate}:override",
            rationale=reason,
            gate_checks=[f"{gate}:overridden"],
            outcome=Outcome.OVERRIDDEN,
            metadata={"gate": gate, **(metadata or {})},
        )


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


class GateTelemetrySummary(BaseModel):
    """Snapshot of gate telemetry across all gates and per-gate breakdown."""

    total_blocks: int = Field(ge=0)
    total_overrides: int = Field(ge=0)
    override_rate: float = Field(
        ge=0.0,
        le=1.0,
        description="overrides / (overrides + blocks); 0 if no data.",
    )
    per_gate: dict[str, tuple[int, int]] = Field(
        default_factory=dict,
        description="gate -> (blocks, overrides)",
    )

    @classmethod
    def empty(cls) -> GateTelemetrySummary:
        return cls(total_blocks=0, total_overrides=0, override_rate=0.0)


def _count(
    registry: MetricsRegistry,
    name: str,
) -> dict[str, int]:
    """Sum counters by gate label from a MetricsRegistry."""
    out: dict[str, int] = {}
    snap = registry.snapshot()
    for row in snap["counters"]:
        if row["name"] != name:
            continue
        gate = row["labels"].get("gate", "unknown")
        out[gate] = out.get(gate, 0) + int(row["value"])
    return out


def summarize(registry: MetricsRegistry) -> GateTelemetrySummary:
    """Build a summary. Also updates the GATE_OVERRIDE_RATE gauge on the
    registry so it's visible on Prometheus scrapes.
    """
    blocks = _count(registry, GATE_BLOCKS_TOTAL)
    overrides = _count(registry, GATE_OVERRIDES_TOTAL)

    total_b = sum(blocks.values())
    total_o = sum(overrides.values())
    denom = total_b + total_o
    rate = (total_o / denom) if denom > 0 else 0.0

    per_gate: dict[str, tuple[int, int]] = {}
    for gate in set(blocks) | set(overrides):
        per_gate[gate] = (blocks.get(gate, 0), overrides.get(gate, 0))

    registry.gauge(GATE_OVERRIDE_RATE, rate)

    return GateTelemetrySummary(
        total_blocks=total_b,
        total_overrides=total_o,
        override_rate=round(rate, 4),
        per_gate=per_gate,
    )
