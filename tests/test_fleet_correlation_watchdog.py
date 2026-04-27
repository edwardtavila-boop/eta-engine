"""Tests for obs.fleet_correlation_watchdog -- the periodic scanner
that walks fleet_corr_partner pairs and emits GRADER events. Covers
the quant-sage 2026-04-27 spec: ETH+BTC on the same crypto_orb may
be one strategy not two; the watchdog surfaces the verdict.
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
from eta_engine.obs.fleet_correlation_watchdog import (
    _partner_pairs,
    assess_pair,
    run_once,
)
from eta_engine.strategies.per_bot_registry import (
    StrategyAssignment,
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
        symbol="BTC",
        side="BUY",
        qty=1.0,
        entry_price=50_000.0,
        exit_price=50_000.0 + pnl_r * 100.0,
        pnl_r=pnl_r,
        pnl_usd=pnl_r * 100.0,
        confluence_score=7.5,
        leverage_used=1.0,
        max_drawdown_during=5.0,
    )


def _seed_trades(journal: DecisionJournal, strategy_id: str, pnl_rs: list[float]) -> None:
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
# _partner_pairs
# ---------------------------------------------------------------------------


def _assignment(bot_id: str, partner: str | None = None) -> StrategyAssignment:
    extras: dict[str, object] = {}
    if partner is not None:
        extras["fleet_corr_partner"] = partner
    return StrategyAssignment(
        bot_id=bot_id,
        strategy_id=f"{bot_id}_v1",
        symbol="BTC",
        timeframe="1h",
        scorer_name="btc",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=60,
        step_days=30,
        min_trades_per_window=10,
        rationale="test fixture for fleet_correlation_watchdog tests; "
                  "needs to be at least 50 chars long to satisfy the registry rationale check.",
        extras=extras,
    )


def test_partner_pairs_skips_bots_without_partner() -> None:
    pairs = _partner_pairs([
        _assignment("solo_bot", partner=None),
    ])
    assert pairs == []


def test_partner_pairs_dedupes_bidirectional_links() -> None:
    pairs = _partner_pairs([
        _assignment("btc_hybrid", partner="eth_perp"),
        _assignment("eth_perp", partner="btc_hybrid"),
    ])
    assert len(pairs) == 1
    bots = sorted(pairs[0])
    assert bots == ["btc_hybrid", "eth_perp"]


def test_partner_pairs_drops_dangling_references() -> None:
    """Partner that isn't in the registry -> silently skipped."""
    pairs = _partner_pairs([
        _assignment("btc_hybrid", partner="ghost_bot"),
    ])
    assert pairs == []


def test_partner_pairs_drops_self_reference() -> None:
    pairs = _partner_pairs([
        _assignment("btc_hybrid", partner="btc_hybrid"),
    ])
    assert pairs == []


# ---------------------------------------------------------------------------
# assess_pair (uses real registry assignments)
# ---------------------------------------------------------------------------


def test_assess_pair_returns_green_on_insufficient_sample(journal: DecisionJournal) -> None:
    """Real registry: btc_hybrid + eth_perp are crypto_orb partners.
    With no journal trades, the assessment is green/insufficient."""
    a = assess_pair(journal=journal, bot_a="btc_hybrid", bot_b="eth_perp")
    assert a.severity == "green"
    assert a.bot_a == "btc_hybrid"
    assert a.bot_b == "eth_perp"
    assert any("insufficient sample" in r for r in a.reasons)


def test_assess_pair_red_on_perfectly_correlated_streams(journal: DecisionJournal) -> None:
    """Same R-stream on both strategies -> rho=1.0 -> red."""
    rs = [1.0, -0.5, 0.7, -0.4, 1.2, 0.9, -0.6, 1.1, -0.3, 0.8, 1.0, -0.7]
    # Pull strategy_ids for the canonical pair
    from eta_engine.strategies.per_bot_registry import get_for_bot
    sid_a = get_for_bot("btc_hybrid").strategy_id  # type: ignore[union-attr]
    sid_b = get_for_bot("eth_perp").strategy_id  # type: ignore[union-attr]
    _seed_trades(journal, sid_a, rs)
    _seed_trades(journal, sid_b, rs)
    a = assess_pair(journal=journal, bot_a="btc_hybrid", bot_b="eth_perp")
    assert a.severity == "red"
    assert a.recommended_action == "merge_for_risk"
    assert a.rho == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# run_once -- integrates registry walk + journal write
# ---------------------------------------------------------------------------


def test_run_once_writes_green_event_when_no_trades(journal: DecisionJournal) -> None:
    out = run_once(journal=journal, write_event=True)
    # All declared partner pairs assessed; events written
    assert len(out) >= 1
    grader_events = [e for e in journal.iter_all() if e.actor == Actor.GRADER]
    assert len(grader_events) == len(out)
    for e in grader_events:
        assert e.outcome == Outcome.NOTED  # green -> NOTED


def test_run_once_dry_run_does_not_write_events(journal: DecisionJournal) -> None:
    out = run_once(journal=journal, write_event=False)
    assert out  # at least one pair assessed
    grader_events = [e for e in journal.iter_all() if e.actor == Actor.GRADER]
    assert grader_events == []


def test_run_once_red_severity_writes_blocked_event(journal: DecisionJournal) -> None:
    """Seed correlated trades for a real registry pair -> watchdog
    emits a BLOCKED-outcome GRADER event recording the trip."""
    rs = [1.0, -0.5, 0.7, -0.4, 1.2, 0.9, -0.6, 1.1, -0.3, 0.8, 1.0, -0.7]
    from eta_engine.strategies.per_bot_registry import get_for_bot
    sid_a = get_for_bot("btc_hybrid").strategy_id  # type: ignore[union-attr]
    sid_b = get_for_bot("eth_perp").strategy_id  # type: ignore[union-attr]
    _seed_trades(journal, sid_a, rs)
    _seed_trades(journal, sid_b, rs)
    run_once(journal=journal, write_event=True)
    grader_events = [
        e for e in journal.iter_all()
        if e.actor == Actor.GRADER and "btc_hybrid" in e.intent
    ]
    assert any(e.outcome == Outcome.BLOCKED for e in grader_events)
    # Verdict metadata carries the structured action
    blocked = next(e for e in grader_events if e.outcome == Outcome.BLOCKED)
    assert blocked.metadata.get("recommended_action") == "merge_for_risk"
    assert blocked.metadata.get("severity") == "red"
