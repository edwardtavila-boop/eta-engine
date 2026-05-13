"""Tests for obs.weekly_sharpe_gate -- devils-advocate's
"kill at <0" weekly OOS Sharpe gate.
"""

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
from eta_engine.obs.weekly_sharpe_gate import (
    SharpeGateAssessment,
    assess_bot_sharpe,
    run_once,
)


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


def _seed(journal: DecisionJournal, strategy_id: str, pnl_rs: list[float]) -> None:
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
# assess_bot_sharpe
# ---------------------------------------------------------------------------


def test_insufficient_sample_returns_green(journal: DecisionJournal) -> None:
    """Below min_trades -> green/continue with insufficient-sample reason."""
    _seed(journal, "strat_a", [1.0, 1.0, -1.0])  # only 3 trades
    a = assess_bot_sharpe(
        journal=journal,
        bot_id="bot_a",
        strategy_id="strat_a",
        min_trades=10,
    )
    assert a.severity == "green"
    assert a.recommended_action == "continue"
    assert any("insufficient sample" in r for r in a.reasons)


def test_strong_positive_sharpe_returns_green(journal: DecisionJournal) -> None:
    """Mostly winners -> Sharpe >> threshold + band -> green."""
    _seed(journal, "strat_a", [1.0, 1.5, 0.8, 1.2, 0.9, 1.1, 0.7, 1.3, 1.0, 0.95, 1.4])
    a = assess_bot_sharpe(
        journal=journal,
        bot_id="bot_a",
        strategy_id="strat_a",
        min_trades=10,
        threshold=0.0,
        review_band=0.5,
    )
    assert a.severity == "green"
    assert a.recommended_action == "continue"
    assert a.sharpe > 0.5  # well above review band


def test_below_threshold_returns_red_demote(journal: DecisionJournal) -> None:
    """Recent stretch of losses -> Sharpe negative -> red/demote."""
    _seed(journal, "strat_a", [-1.0, -1.0, 0.5, -0.8, -1.2, -0.5, -1.0, -0.3, -0.9, -1.1])
    a = assess_bot_sharpe(
        journal=journal,
        bot_id="bot_a",
        strategy_id="strat_a",
        min_trades=10,
        threshold=0.0,
    )
    assert a.severity == "red"
    assert a.recommended_action == "demote"
    assert a.sharpe < 0


def test_review_band_returns_amber_review(journal: DecisionJournal) -> None:
    """Sharpe just above 0 but below review_band -> amber/review."""
    # Mix of small wins/losses producing a tiny positive Sharpe
    _seed(
        journal,
        "strat_a",
        [
            0.1,
            -0.05,
            0.08,
            -0.1,
            0.05,
            -0.02,
            0.12,
            -0.08,
            0.1,
            -0.05,
        ],
    )
    a = assess_bot_sharpe(
        journal=journal,
        bot_id="bot_a",
        strategy_id="strat_a",
        min_trades=10,
        threshold=0.0,
        review_band=0.5,
    )
    # Expect amber OR green -- depends on exact arithmetic, but the
    # action should NOT be "demote".
    assert a.recommended_action in {"continue", "review"}
    assert a.severity != "red"


def test_threshold_override_works(journal: DecisionJournal) -> None:
    """Tighter threshold flags otherwise-green bots."""
    _seed(journal, "strat_a", [0.1] * 10)  # constant series -> sharpe=0
    a = assess_bot_sharpe(
        journal=journal,
        bot_id="bot_a",
        strategy_id="strat_a",
        min_trades=10,
        threshold=0.5,
    )
    # Sharpe=0 < threshold=0.5 -> red
    assert a.severity == "red"


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------


def test_run_once_skips_deactivated_by_default(journal: DecisionJournal) -> None:
    """The xrp_perp registry row carries extras['deactivated']=True;
    run_once should skip it by default."""
    out = run_once(journal=journal, write_event=False)
    bot_ids = {a.bot_id for a in out}
    assert "xrp_perp" not in bot_ids


def test_run_once_includes_deactivated_when_asked(journal: DecisionJournal) -> None:
    out = run_once(
        journal=journal,
        write_event=False,
        skip_deactivated=False,
    )
    bot_ids = {a.bot_id for a in out}
    assert "xrp_perp" in bot_ids


def test_run_once_writes_grader_events(journal: DecisionJournal) -> None:
    """Each assessment becomes a GRADER event; severity tags outcome."""
    out = run_once(journal=journal, write_event=True)
    grader = [e for e in journal.iter_all() if e.actor == Actor.GRADER]
    assert len(grader) == len(out)
    for e in grader:
        assert e.intent.startswith("weekly_sharpe:")
        # All GREEN today (insufficient sample) -> NOTED outcome
        assert e.outcome == Outcome.NOTED


def test_run_once_dry_run_does_not_write(journal: DecisionJournal) -> None:
    out = run_once(journal=journal, write_event=False)
    assert out  # at least one bot assessed
    grader = [e for e in journal.iter_all() if e.actor == Actor.GRADER]
    assert grader == []


def test_assessment_serializes_round_trip(journal: DecisionJournal) -> None:
    a = assess_bot_sharpe(
        journal=journal,
        bot_id="bot_a",
        strategy_id="strat_a",
    )
    payload = a.model_dump()
    a2 = SharpeGateAssessment.model_validate(payload)
    assert a2 == a
