"""Tests for obs.drift_watchdog — journal -> assessment -> GRADER event."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eta_engine.backtest.models import Trade
from eta_engine.obs.decision_journal import (
    Actor,
    DecisionJournal,
    JournalEvent,
    Outcome,
)
from eta_engine.obs.drift_monitor import BaselineSnapshot
from eta_engine.obs.drift_watchdog import (
    run_all,
    run_once,
    trades_from_journal,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def journal(tmp_path) -> DecisionJournal:  # type: ignore[no-untyped-def]
    return DecisionJournal(tmp_path / "journal.jsonl", supabase_mirror=False)


def _trade(pnl_r: float) -> Trade:
    return Trade(
        entry_time=datetime(2026, 1, 1, tzinfo=UTC),
        exit_time=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
        symbol="MNQ",
        side="BUY",
        qty=1.0,
        entry_price=21000.0,
        exit_price=21000.0 + pnl_r * 10.0,
        pnl_r=pnl_r,
        pnl_usd=pnl_r * 100.0,
        confluence_score=7.5,
        leverage_used=1.0,
        max_drawdown_during=5.0,
    )


def _seed_executed_trades(
    journal: DecisionJournal,
    strategy_id: str,
    pnl_rs: list[float],
) -> None:
    """Append N TRADE_ENGINE/EXECUTED events with serialized Trade payloads."""
    for r in pnl_rs:
        t = _trade(r)
        journal.append(
            JournalEvent(
                actor=Actor.TRADE_ENGINE,
                intent="trade_executed",
                outcome=Outcome.EXECUTED,
                metadata={
                    "strategy_id": strategy_id,
                    "trade": t.model_dump(mode="json"),
                },
            )
        )


# ---------------------------------------------------------------------------
# trades_from_journal
# ---------------------------------------------------------------------------


def test_trades_from_journal_empty(journal: DecisionJournal) -> None:
    assert trades_from_journal(journal, strategy_id="s") == []


def test_trades_from_journal_filters_by_strategy(journal: DecisionJournal) -> None:
    _seed_executed_trades(journal, "alpha", [1.0, 1.0, -1.0])
    _seed_executed_trades(journal, "beta", [-1.0, 1.0])
    assert len(trades_from_journal(journal, strategy_id="alpha")) == 3
    assert len(trades_from_journal(journal, strategy_id="beta")) == 2


def test_trades_from_journal_skips_non_trade_events(journal: DecisionJournal) -> None:
    journal.append(
        JournalEvent(
            actor=Actor.KILL_SWITCH,
            intent="dd_breach",
            outcome=Outcome.EXECUTED,
            metadata={"strategy_id": "alpha"},
        )
    )
    _seed_executed_trades(journal, "alpha", [1.0])
    # Only the TRADE_ENGINE/EXECUTED row should be picked up.
    assert len(trades_from_journal(journal, strategy_id="alpha")) == 1


def test_trades_from_journal_skips_blocked_trades(journal: DecisionJournal) -> None:
    """A TRADE_ENGINE event with BLOCKED outcome (e.g. risk-gate veto)
    must not show up as a completed trade."""
    journal.append(
        JournalEvent(
            actor=Actor.TRADE_ENGINE,
            intent="entry_blocked",
            outcome=Outcome.BLOCKED,
            metadata={"strategy_id": "alpha", "trade": _trade(0.0).model_dump(mode="json")},
        )
    )
    _seed_executed_trades(journal, "alpha", [1.0])
    assert len(trades_from_journal(journal, strategy_id="alpha")) == 1


def test_trades_from_journal_ignores_invalid_payloads(journal: DecisionJournal) -> None:
    journal.append(
        JournalEvent(
            actor=Actor.TRADE_ENGINE,
            intent="trade_executed",
            outcome=Outcome.EXECUTED,
            metadata={"strategy_id": "alpha", "trade": {"not": "a trade"}},
        )
    )
    _seed_executed_trades(journal, "alpha", [1.5])
    assert len(trades_from_journal(journal, strategy_id="alpha")) == 1


def test_trades_from_journal_last_n_takes_tail(journal: DecisionJournal) -> None:
    _seed_executed_trades(journal, "alpha", [float(i) for i in range(10)])
    out = trades_from_journal(journal, strategy_id="alpha", last_n=3)
    assert [t.pnl_r for t in out] == [7.0, 8.0, 9.0]


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------


def test_run_once_insufficient_sample_writes_green_event(journal: DecisionJournal) -> None:
    bl = BaselineSnapshot(strategy_id="alpha", n_trades=200, win_rate=0.6, avg_r=0.4, r_stddev=1.0)
    _seed_executed_trades(journal, "alpha", [1.5] * 5)
    a = run_once(journal=journal, strategy_id="alpha", baseline=bl, min_trades=20)
    assert a.severity == "green"
    grader_events = [e for e in journal.read_all() if e.actor == Actor.GRADER]
    assert len(grader_events) == 1
    assert grader_events[0].outcome == Outcome.NOTED
    assert grader_events[0].metadata["severity"] == "green"


def test_run_once_red_severity_writes_blocked_event(journal: DecisionJournal) -> None:
    bl = BaselineSnapshot(strategy_id="alpha", n_trades=500, win_rate=0.6, avg_r=0.4, r_stddev=1.0)
    # 30 trades, mostly losses → win-rate collapse
    pnl_rs = [1.5] * 5 + [-1.0] * 25
    _seed_executed_trades(journal, "alpha", pnl_rs)
    a = run_once(journal=journal, strategy_id="alpha", baseline=bl)
    assert a.severity == "red"
    grader_events = [e for e in journal.read_all() if e.actor == Actor.GRADER]
    assert len(grader_events) == 1
    assert grader_events[0].outcome == Outcome.BLOCKED
    assert grader_events[0].metadata["severity"] == "red"
    assert grader_events[0].rationale  # non-empty


def test_run_once_dry_run_does_not_write(journal: DecisionJournal) -> None:
    bl = BaselineSnapshot(strategy_id="alpha", n_trades=200, win_rate=0.6, avg_r=0.4, r_stddev=1.0)
    _seed_executed_trades(journal, "alpha", [1.5] * 5)
    before = sum(1 for _ in journal.iter_all())
    a = run_once(journal=journal, strategy_id="alpha", baseline=bl, write_event=False)
    after = sum(1 for _ in journal.iter_all())
    assert a is not None
    assert before == after  # no new event appended


def test_run_once_event_metadata_round_trips(journal: DecisionJournal) -> None:
    bl = BaselineSnapshot(strategy_id="alpha", n_trades=200, win_rate=0.6, avg_r=0.4, r_stddev=1.0)
    _seed_executed_trades(journal, "alpha", [1.5] * 22)
    run_once(journal=journal, strategy_id="alpha", baseline=bl)
    grader = next(e for e in journal.read_all() if e.actor == Actor.GRADER)
    md = grader.metadata
    for key in (
        "severity",
        "win_rate_z",
        "avg_r_z",
        "recent_win_rate",
        "recent_avg_r",
        "baseline_win_rate",
        "baseline_avg_r",
    ):
        assert key in md, f"missing metadata key: {key}"


# ---------------------------------------------------------------------------
# run_all
# ---------------------------------------------------------------------------


def test_run_all_returns_per_strategy_assessments(journal: DecisionJournal) -> None:
    _seed_executed_trades(journal, "alpha", [1.5] * 22)
    _seed_executed_trades(journal, "beta", [-1.0] * 22)
    bl_alpha = BaselineSnapshot(strategy_id="alpha", n_trades=100, win_rate=0.7, avg_r=0.5, r_stddev=1.0)
    bl_beta = BaselineSnapshot(strategy_id="beta", n_trades=100, win_rate=0.6, avg_r=0.4, r_stddev=1.0)
    out = run_all(
        journal=journal,
        strategy_baselines=[("alpha", bl_alpha), ("beta", bl_beta)],
    )
    assert set(out) == {"alpha", "beta"}
    # Beta has all losers — should escalate
    assert out["beta"].severity in ("amber", "red")
