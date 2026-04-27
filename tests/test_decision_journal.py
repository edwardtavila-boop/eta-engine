"""Tests for obs.decision_journal -- unified decision log."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from pydantic import ValidationError

from eta_engine.obs.decision_journal import (
    Actor,
    DecisionJournal,
    JournalEvent,
    Outcome,
)

_T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "journal.jsonl"


@pytest.fixture
def journal(journal_path: Path) -> DecisionJournal:
    return DecisionJournal(journal_path)


# --------------------------------------------------------------------------- #
# JournalEvent validation
# --------------------------------------------------------------------------- #


def test_event_rejects_empty_intent() -> None:
    with pytest.raises(ValidationError):
        JournalEvent(actor=Actor.TRADE_ENGINE, intent="")


def test_event_defaults_ts_to_utc_now() -> None:
    e = JournalEvent(actor=Actor.OPERATOR, intent="ack")
    assert e.ts.tzinfo is not None


def test_event_default_outcome_is_noted() -> None:
    e = JournalEvent(actor=Actor.OPERATOR, intent="x")
    assert e.outcome == Outcome.NOTED


# --------------------------------------------------------------------------- #
# DecisionJournal append/read
# --------------------------------------------------------------------------- #


def test_empty_journal_has_zero_length(journal: DecisionJournal) -> None:
    assert len(journal) == 0
    assert journal.read_all() == []


def test_append_one_and_read_back(journal: DecisionJournal) -> None:
    journal.record(
        ts=_T0,
        actor=Actor.TRADE_ENGINE,
        intent="open_mnq_long",
        rationale="confluence=8 regime=TRENDING",
        outcome=Outcome.EXECUTED,
    )
    events = journal.read_all()
    assert len(events) == 1
    assert events[0].actor == Actor.TRADE_ENGINE
    assert events[0].intent == "open_mnq_long"
    assert events[0].outcome == Outcome.EXECUTED


def test_append_is_append_only(journal: DecisionJournal) -> None:
    journal.record(ts=_T0, actor=Actor.KILL_SWITCH, intent="armed")
    journal.record(ts=_T0 + timedelta(seconds=1), actor=Actor.OPERATOR, intent="ack")
    journal.record(ts=_T0 + timedelta(seconds=2), actor=Actor.FIRM_BOARD, intent="verdict_green")
    assert len(journal) == 3


def test_read_all_preserves_order(journal: DecisionJournal) -> None:
    for i in range(5):
        journal.record(
            ts=_T0 + timedelta(seconds=i),
            actor=Actor.TRADE_ENGINE,
            intent=f"tick_{i}",
        )
    intents = [e.intent for e in journal.read_all()]
    assert intents == ["tick_0", "tick_1", "tick_2", "tick_3", "tick_4"]


def test_round_trip_preserves_metadata_links_gates(journal: DecisionJournal) -> None:
    journal.record(
        ts=_T0,
        actor=Actor.RISK_GATE,
        intent="veto_low_confluence",
        rationale="score=3 < threshold=7",
        gate_checks=["+regime", "-confluence", "+session"],
        outcome=Outcome.BLOCKED,
        links=["spec-123", "trade-456"],
        metadata={"score": 3, "threshold": 7, "regime": "RANGING"},
    )
    e = journal.read_all()[0]
    assert e.gate_checks == ["+regime", "-confluence", "+session"]
    assert e.links == ["spec-123", "trade-456"]
    assert e.metadata == {"score": 3, "threshold": 7, "regime": "RANGING"}


def test_iter_all_yields_same_as_read_all(journal: DecisionJournal) -> None:
    for i in range(3):
        journal.record(ts=_T0 + timedelta(seconds=i), actor=Actor.TRADE_ENGINE, intent=f"e{i}")
    via_iter = [e.intent for e in journal.iter_all()]
    via_all = [e.intent for e in journal.read_all()]
    assert via_iter == via_all


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


def test_read_since_filters_by_timestamp(journal: DecisionJournal) -> None:
    for i in range(5):
        journal.record(ts=_T0 + timedelta(hours=i), actor=Actor.TRADE_ENGINE, intent=f"t{i}")
    cutoff = _T0 + timedelta(hours=2)
    kept = journal.read_since(cutoff)
    assert [e.intent for e in kept] == ["t2", "t3", "t4"]


def test_read_by_actor(journal: DecisionJournal) -> None:
    journal.record(ts=_T0, actor=Actor.TRADE_ENGINE, intent="x")
    journal.record(ts=_T0, actor=Actor.KILL_SWITCH, intent="y")
    journal.record(ts=_T0, actor=Actor.TRADE_ENGINE, intent="z")
    te = journal.read_by_actor(Actor.TRADE_ENGINE)
    assert [e.intent for e in te] == ["x", "z"]


def test_read_by_outcome(journal: DecisionJournal) -> None:
    journal.record(ts=_T0, actor=Actor.TRADE_ENGINE, intent="a", outcome=Outcome.EXECUTED)
    journal.record(ts=_T0, actor=Actor.RISK_GATE, intent="b", outcome=Outcome.BLOCKED)
    journal.record(ts=_T0, actor=Actor.OPERATOR, intent="c", outcome=Outcome.OVERRIDDEN)
    assert len(journal.read_by_outcome(Outcome.BLOCKED)) == 1
    assert len(journal.read_by_outcome(Outcome.EXECUTED)) == 1
    assert len(journal.read_by_outcome(Outcome.OVERRIDDEN)) == 1


# --------------------------------------------------------------------------- #
# Aggregations
# --------------------------------------------------------------------------- #


def test_outcome_counts(journal: DecisionJournal) -> None:
    for _ in range(3):
        journal.record(ts=_T0, actor=Actor.TRADE_ENGINE, intent="x", outcome=Outcome.EXECUTED)
    for _ in range(2):
        journal.record(ts=_T0, actor=Actor.RISK_GATE, intent="y", outcome=Outcome.BLOCKED)
    counts = journal.outcome_counts()
    assert counts[Outcome.EXECUTED] == 3
    assert counts[Outcome.BLOCKED] == 2
    assert counts[Outcome.NOTED] == 0


def test_actor_counts(journal: DecisionJournal) -> None:
    journal.record(ts=_T0, actor=Actor.JARVIS, intent="x")
    journal.record(ts=_T0, actor=Actor.JARVIS, intent="y")
    journal.record(ts=_T0, actor=Actor.TRADE_ENGINE, intent="z")
    counts = journal.actor_counts()
    assert counts[Actor.JARVIS] == 2
    assert counts[Actor.TRADE_ENGINE] == 1


def test_override_rate_zero_when_no_gate_events(journal: DecisionJournal) -> None:
    journal.record(ts=_T0, actor=Actor.TRADE_ENGINE, intent="x")
    assert journal.override_rate() == 0.0


def test_override_rate_counts_overrides_over_gate_events(
    journal: DecisionJournal,
) -> None:
    # 3 gate events, 1 overridden = 33%
    journal.record(ts=_T0, actor=Actor.RISK_GATE, intent="v", outcome=Outcome.BLOCKED)
    journal.record(ts=_T0, actor=Actor.RISK_GATE, intent="v", outcome=Outcome.OVERRIDDEN)
    journal.record(ts=_T0, actor=Actor.KILL_SWITCH, intent="k", outcome=Outcome.BLOCKED)
    # Non-gate event should not count
    journal.record(ts=_T0, actor=Actor.TRADE_ENGINE, intent="x", outcome=Outcome.OVERRIDDEN)
    assert journal.override_rate() == pytest.approx(1 / 3)


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #


def test_malformed_lines_skipped(journal: DecisionJournal, journal_path: Path) -> None:
    journal.record(ts=_T0, actor=Actor.TRADE_ENGINE, intent="good")
    # Inject garbage + empty lines
    with journal_path.open("a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
        fh.write("\n")
        fh.write('{"incomplete": \n')
    journal.record(ts=_T0, actor=Actor.TRADE_ENGINE, intent="also_good")
    events = journal.read_all()
    assert [e.intent for e in events] == ["good", "also_good"]


def test_journal_autocreates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "path" / "journal.jsonl"
    j = DecisionJournal(nested)
    j.record(ts=_T0, actor=Actor.TRADE_ENGINE, intent="x")
    assert nested.exists()
    assert len(j) == 1


def test_string_path_accepted(tmp_path: Path) -> None:
    p = tmp_path / "j.jsonl"
    j = DecisionJournal(str(p))
    j.record(ts=_T0, actor=Actor.TRADE_ENGINE, intent="x")
    assert len(j) == 1


def test_append_returns_event(journal: DecisionJournal) -> None:
    ev = JournalEvent(ts=_T0, actor=Actor.TRADE_ENGINE, intent="x")
    returned = journal.append(ev)
    assert returned is ev


# --------------------------------------------------------------------------- #
# Default singleton
# --------------------------------------------------------------------------- #


def test_default_journal_is_singleton() -> None:
    from eta_engine.obs.decision_journal import default_journal

    a = default_journal()
    b = default_journal()
    assert a is b
