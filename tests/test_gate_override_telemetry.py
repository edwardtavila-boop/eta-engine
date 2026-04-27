"""Tests for obs.gate_override_telemetry."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from eta_engine.obs.decision_journal import Actor, DecisionJournal, Outcome
from eta_engine.obs.gate_override_telemetry import (
    GateTelemetrySummary,
    record_gate_block,
    record_gate_override,
    summarize,
)
from eta_engine.obs.metrics import (
    GATE_BLOCKS_TOTAL,
    GATE_OVERRIDE_RATE,
    GATE_OVERRIDES_TOTAL,
    MetricsRegistry,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def registry() -> MetricsRegistry:
    return MetricsRegistry()


@pytest.fixture
def journal(tmp_path: Path) -> DecisionJournal:
    return DecisionJournal(tmp_path / "j.jsonl")


# --------------------------------------------------------------------------- #
# record_gate_block
# --------------------------------------------------------------------------- #


def test_block_bumps_counter(registry: MetricsRegistry) -> None:
    record_gate_block(
        gate="kill_switch",
        reason="daily dd exceeded",
        actor=Actor.KILL_SWITCH,
        registry=registry,
    )
    assert (
        registry.get_counter(
            GATE_BLOCKS_TOTAL,
            labels={"gate": "kill_switch"},
        )
        == 1.0
    )


def test_block_also_writes_journal(
    registry: MetricsRegistry,
    journal: DecisionJournal,
) -> None:
    record_gate_block(
        gate="kill_switch",
        reason="dd exceeded",
        actor=Actor.KILL_SWITCH,
        registry=registry,
        journal=journal,
    )
    events = journal.read_all()
    assert len(events) == 1
    assert events[0].outcome == Outcome.BLOCKED
    assert "gate:kill_switch:block" in events[0].intent


def test_block_rejects_empty_gate(registry: MetricsRegistry) -> None:
    with pytest.raises(ValueError, match="gate"):
        record_gate_block(
            gate="",
            reason="x",
            actor=Actor.KILL_SWITCH,
            registry=registry,
        )


def test_block_rejects_empty_reason(registry: MetricsRegistry) -> None:
    with pytest.raises(ValueError, match="reason"):
        record_gate_block(
            gate="g",
            reason="",
            actor=Actor.KILL_SWITCH,
            registry=registry,
        )


# --------------------------------------------------------------------------- #
# record_gate_override
# --------------------------------------------------------------------------- #


def test_override_bumps_counter(registry: MetricsRegistry) -> None:
    record_gate_override(
        gate="kill_switch",
        reason="operator override -- small size trade",
        actor=Actor.OPERATOR,
        registry=registry,
    )
    assert (
        registry.get_counter(
            GATE_OVERRIDES_TOTAL,
            labels={"gate": "kill_switch"},
        )
        == 1.0
    )


def test_override_writes_journal(
    registry: MetricsRegistry,
    journal: DecisionJournal,
) -> None:
    record_gate_override(
        gate="confluence_gate",
        reason="operator override",
        actor=Actor.OPERATOR,
        registry=registry,
        journal=journal,
    )
    events = journal.read_all()
    assert len(events) == 1
    assert events[0].outcome == Outcome.OVERRIDDEN
    assert events[0].actor == Actor.OPERATOR


def test_override_rejects_empty_gate(registry: MetricsRegistry) -> None:
    with pytest.raises(ValueError):
        record_gate_override(
            gate="",
            reason="x",
            actor=Actor.OPERATOR,
            registry=registry,
        )


# --------------------------------------------------------------------------- #
# summarize
# --------------------------------------------------------------------------- #


def test_summarize_empty(registry: MetricsRegistry) -> None:
    s = summarize(registry)
    assert s.total_blocks == 0
    assert s.total_overrides == 0
    assert s.override_rate == 0.0
    assert s.per_gate == {}


def test_summarize_mix(registry: MetricsRegistry) -> None:
    # 4 blocks across two gates, 1 override
    record_gate_block(
        gate="g1",
        reason="r",
        actor=Actor.KILL_SWITCH,
        registry=registry,
    )
    record_gate_block(
        gate="g1",
        reason="r",
        actor=Actor.KILL_SWITCH,
        registry=registry,
    )
    record_gate_block(
        gate="g2",
        reason="r",
        actor=Actor.RISK_GATE,
        registry=registry,
    )
    record_gate_block(
        gate="g2",
        reason="r",
        actor=Actor.RISK_GATE,
        registry=registry,
    )
    record_gate_override(
        gate="g1",
        reason="r",
        actor=Actor.OPERATOR,
        registry=registry,
    )

    s = summarize(registry)
    assert s.total_blocks == 4
    assert s.total_overrides == 1
    assert s.override_rate == pytest.approx(1 / 5, abs=0.001)
    assert s.per_gate["g1"] == (2, 1)
    assert s.per_gate["g2"] == (2, 0)


def test_summarize_updates_rate_gauge(registry: MetricsRegistry) -> None:
    record_gate_block(
        gate="g",
        reason="r",
        actor=Actor.KILL_SWITCH,
        registry=registry,
    )
    record_gate_override(
        gate="g",
        reason="r",
        actor=Actor.OPERATOR,
        registry=registry,
    )
    summarize(registry)
    rate = registry.get_gauge(GATE_OVERRIDE_RATE)
    assert rate == pytest.approx(0.5, abs=0.001)


def test_summary_empty_factory() -> None:
    s = GateTelemetrySummary.empty()
    assert s.total_blocks == 0
    assert s.override_rate == 0.0


def test_override_rate_capped_1() -> None:
    # If there are only overrides and no blocks -- override_rate == 1.0
    r = MetricsRegistry()
    record_gate_override(
        gate="g",
        reason="r",
        actor=Actor.OPERATOR,
        registry=r,
    )
    s = summarize(r)
    assert s.override_rate == 1.0
