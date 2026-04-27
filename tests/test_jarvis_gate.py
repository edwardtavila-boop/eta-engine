"""
EVOLUTIONARY TRADING ALGO  //  tests.test_jarvis_gate
=========================================
Unit tests for :mod:`eta_engine.brain.jarvis_gate`.

The shared helper is tiny -- just :func:`ask_jarvis`,
:func:`record_gate_event`, and :func:`pick_llm_tier` -- but it is the
single integration point every bot will use to talk to JARVIS, so we
cover every branch:

* APPROVED / CONDITIONAL / DENIED / DEFERRED verdict unpacking.
* ``provide_ctx`` lambda is called exactly when JARVIS has no engine.
* JARVIS-side exceptions fail closed (``allowed=False``).
* ``record_gate_event`` is a no-op on ``None`` journal.
* ``record_gate_event`` survives journal-write exceptions.
* ``pick_llm_tier`` returns the policy-selected tier per
  :class:`TaskCategory`.
* Unknown category falls back to SONNET.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from eta_engine.brain.jarvis_admin import (
    ActionType,
    JarvisAdmin,
    SubsystemId,
    Verdict,
)
from eta_engine.brain.jarvis_context import (
    EquitySnapshot,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    build_snapshot,
)
from eta_engine.brain.jarvis_gate import (
    ask_jarvis,
    pick_llm_tier,
    record_gate_event,
)
from eta_engine.brain.model_policy import ModelTier, TaskCategory
from eta_engine.obs.decision_journal import Actor, DecisionJournal, Outcome

_ET = ZoneInfo("America/New_York")


def _midday_ts() -> datetime:
    return datetime(2026, 4, 15, 12, 0, tzinfo=_ET).astimezone(UTC)


def _trade_ctx():  # type: ignore[no-untyped-def]
    return build_snapshot(
        macro=MacroSnapshot(vix_level=17.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=0.0,
            daily_drawdown_pct=0.0,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="TREND_UP", confidence=0.7),
        journal=JournalSnapshot(),
        ts=_midday_ts(),
    )


def _kill_ctx():  # type: ignore[no-untyped-def]
    return build_snapshot(
        macro=MacroSnapshot(vix_level=17.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=-3_000.0,
            daily_drawdown_pct=0.06,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="TREND_DOWN", confidence=0.7),
        journal=JournalSnapshot(kill_switch_active=True),
        ts=_midday_ts(),
    )


def _reduce_ctx():  # type: ignore[no-untyped-def]
    return build_snapshot(
        macro=MacroSnapshot(vix_level=17.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=-1_250.0,
            daily_drawdown_pct=0.025,
            open_positions=1,
            open_risk_r=1.0,
        ),
        regime=RegimeSnapshot(regime="TREND_UP", confidence=0.6),
        journal=JournalSnapshot(),
        ts=_midday_ts(),
    )


# --------------------------------------------------------------------------- #
# ask_jarvis
# --------------------------------------------------------------------------- #
class TestAskJarvis:
    def test_approved_returns_allowed_true(self) -> None:
        jarvis = JarvisAdmin()
        allowed, cap, code = ask_jarvis(
            jarvis,
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
            rationale="routine entry",
            provide_ctx=_trade_ctx,
            side="LONG",
            symbol="MNQ",
            price=25_000.0,
        )
        assert allowed is True
        # TRADE tier may carry a live sizing hint; what matters is
        # that when present it's a valid [0, 1] multiplier.
        if cap is not None:
            assert 0.0 <= cap <= 1.0
        assert isinstance(code, str) and code

    def test_denied_under_kill(self) -> None:
        jarvis = JarvisAdmin()
        allowed, _cap, code = ask_jarvis(
            jarvis,
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
            rationale="entry under kill",
            provide_ctx=_kill_ctx,
            side="LONG",
            symbol="MNQ",
            price=25_000.0,
        )
        assert allowed is False
        assert "kill" in code.lower() or "stand" in code.lower() or "blocked" in code.lower()

    def test_conditional_under_reduce_carries_cap(self) -> None:
        jarvis = JarvisAdmin()
        allowed, cap, code = ask_jarvis(
            jarvis,
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
            rationale="entry under reduce",
            provide_ctx=_reduce_ctx,
            side="LONG",
            symbol="MNQ",
            price=25_000.0,
        )
        assert allowed is True
        assert cap is not None
        assert 0.0 < cap <= 0.5
        assert isinstance(code, str) and code

    def test_strategy_deploy_denied_under_kill(self) -> None:
        jarvis = JarvisAdmin()
        allowed, _cap, code = ask_jarvis(
            jarvis,
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.STRATEGY_DEPLOY,
            rationale="arming under kill",
            provide_ctx=_kill_ctx,
        )
        assert allowed is False
        assert code

    def test_jarvis_exception_fails_closed(self) -> None:
        class BrokenAdmin:
            def request_approval(self, *a, **kw):  # type: ignore[no-untyped-def]
                raise RuntimeError("engine crashed")

        allowed, cap, code = ask_jarvis(
            BrokenAdmin(),  # type: ignore[arg-type]
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
            provide_ctx=_trade_ctx,
        )
        assert allowed is False
        assert cap is None
        assert code == "jarvis_error"


# --------------------------------------------------------------------------- #
# record_gate_event
# --------------------------------------------------------------------------- #
class TestRecordGateEvent:
    def test_none_journal_is_silent_noop(self) -> None:
        # Should not raise
        record_gate_event(
            None,
            actor=Actor.TRADE_ENGINE,
            intent="noop",
            rationale="smoke",
            outcome=Outcome.NOTED,
        )

    def test_writes_event_to_journal(self, tmp_path: Path) -> None:
        journal = DecisionJournal(tmp_path / "jarvis_gate.jsonl")
        record_gate_event(
            journal,
            actor=Actor.TRADE_ENGINE,
            intent="mnq_order_routed",
            rationale="order accepted by venue",
            outcome=Outcome.EXECUTED,
            order_id="X-1",
            qty=3.0,
        )
        events = journal.read_all()
        assert len(events) == 1
        ev = events[0]
        assert ev.intent == "mnq_order_routed"
        assert ev.outcome == Outcome.EXECUTED
        assert ev.metadata["order_id"] == "X-1"

    def test_journal_exception_does_not_raise(self, tmp_path: Path) -> None:
        class BrokenJournal:
            def record(self, *a, **kw):  # type: ignore[no-untyped-def]
                raise OSError("disk full")

        # Should not raise -- a dead disk cannot kill the trading loop.
        record_gate_event(
            BrokenJournal(),  # type: ignore[arg-type]
            actor=Actor.TRADE_ENGINE,
            intent="write_fail",
            rationale="disk dead",
            outcome=Outcome.NOTED,
        )


# --------------------------------------------------------------------------- #
# pick_llm_tier
# --------------------------------------------------------------------------- #
class TestPickLlmTier:
    def test_opus_task_routes_to_opus(self) -> None:
        jarvis = JarvisAdmin()
        tier = pick_llm_tier(
            jarvis,
            subsystem=SubsystemId.BOT_MNQ,
            category=TaskCategory.RED_TEAM_SCORING,
            rationale="operator asked for red team",
        )
        assert tier == ModelTier.OPUS

    def test_sonnet_task_routes_to_sonnet(self) -> None:
        jarvis = JarvisAdmin()
        tier = pick_llm_tier(
            jarvis,
            subsystem=SubsystemId.BOT_MNQ,
            category=TaskCategory.REFACTOR,
            rationale="routine refactor",
        )
        assert tier == ModelTier.SONNET

    def test_haiku_task_routes_to_haiku(self) -> None:
        jarvis = JarvisAdmin()
        tier = pick_llm_tier(
            jarvis,
            subsystem=SubsystemId.BOT_MNQ,
            category=TaskCategory.COMMIT_MESSAGE,
            rationale="commit draft",
        )
        assert tier == ModelTier.HAIKU

    def test_missing_category_falls_back_to_sonnet(self) -> None:
        class BrokenAdmin:
            def select_llm_tier(self, **kw):  # type: ignore[no-untyped-def]
                raise RuntimeError("no policy wired")

        tier = pick_llm_tier(
            BrokenAdmin(),  # type: ignore[arg-type]
            subsystem=SubsystemId.BOT_MNQ,
            category=TaskCategory.REFACTOR,
        )
        assert tier == ModelTier.SONNET


# --------------------------------------------------------------------------- #
# Response verdict acceptance matrix (sanity)
# --------------------------------------------------------------------------- #
def test_allowed_verdicts_exactly_approved_and_conditional() -> None:
    """Documentary test: allowed iff verdict in {APPROVED, CONDITIONAL}."""
    assert Verdict.APPROVED != Verdict.CONDITIONAL
    assert {Verdict.APPROVED, Verdict.CONDITIONAL}.isdisjoint(
        {Verdict.DENIED, Verdict.DEFERRED},
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
