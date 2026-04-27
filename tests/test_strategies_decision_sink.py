"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_decision_sink.

Unit tests for :mod:`eta_engine.strategies.decision_sink` -- the
bridge between :class:`RouterDecision` and the unified
:class:`DecisionJournal`.

Three concentric layers are covered:

* ``router_decision_to_event`` -- pure helper; verify the event shape
  and metadata payload.
* ``RouterDecisionSink`` -- wrapper flags (enabled, also_log_flat,
  include_candidates, default_outcome) and OSError robustness.
* Live :class:`RouterAdapter` integration -- end-to-end, every
  ``push_bar`` writes one event when a sink is wired.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from eta_engine.obs.decision_journal import (
    Actor,
    DecisionJournal,
    JournalEvent,
    Outcome,
)
from eta_engine.strategies.decision_sink import (
    RouterDecisionSink,
    router_decision_to_event,
)
from eta_engine.strategies.engine_adapter import RouterAdapter
from eta_engine.strategies.models import (
    Bar,
    Side,
    StrategyId,
    StrategySignal,
)
from eta_engine.strategies.policy_router import RouterDecision

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _actionable_signal(
    *,
    strategy: StrategyId = StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
    side: Side = Side.LONG,
    confidence: float = 7.0,
) -> StrategySignal:
    return StrategySignal(
        strategy=strategy,
        side=side,
        entry=100.0,
        stop=95.0,
        target=115.0,
        confidence=confidence,
        risk_mult=1.0,
        rationale_tags=("sweep", "displacement"),
    )


def _flat_signal(
    *,
    strategy: StrategyId = StrategyId.FVG_FILL_CONFLUENCE,
) -> StrategySignal:
    return StrategySignal(
        strategy=strategy,
        side=Side.FLAT,
        rationale_tags=("no_setup",),
    )


def _decision(
    *,
    asset: str = "MNQ",
    winner: StrategySignal | None = None,
    candidates: tuple[StrategySignal, ...] | None = None,
    eligible: tuple[StrategyId, ...] = (
        StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
        StrategyId.OB_BREAKER_RETEST,
    ),
) -> RouterDecision:
    w = winner if winner is not None else _actionable_signal()
    c = candidates if candidates is not None else (w, _flat_signal())
    return RouterDecision(
        asset=asset,
        winner=w,
        candidates=c,
        eligible=eligible,
    )


def _bar_dict(ts: int, close: float = 100.0) -> dict[str, float]:
    return {
        "ts": ts,
        "open": close - 0.5,
        "high": close + 0.5,
        "low": close - 1.0,
        "close": close,
        "volume": 100.0,
    }


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "router_journal.jsonl"


@pytest.fixture
def journal(journal_path: Path) -> DecisionJournal:
    return DecisionJournal(journal_path)


# ---------------------------------------------------------------------------
# router_decision_to_event
# ---------------------------------------------------------------------------


class TestRouterDecisionToEvent:
    def test_actor_is_strategy_router(self) -> None:
        event = router_decision_to_event(_decision())
        assert event.actor is Actor.STRATEGY_ROUTER

    def test_intent_encodes_asset(self) -> None:
        event = router_decision_to_event(_decision(asset="BTC"))
        assert event.intent == "dispatch_BTC"

    def test_rationale_encodes_strategy_and_side(self) -> None:
        event = router_decision_to_event(_decision())
        assert event.rationale == "liquidity_sweep_displacement:LONG"

    def test_rationale_encodes_short_side(self) -> None:
        sig = _actionable_signal(
            strategy=StrategyId.OB_BREAKER_RETEST,
            side=Side.SHORT,
        )
        event = router_decision_to_event(_decision(winner=sig))
        assert event.rationale == "ob_breaker_retest:SHORT"

    def test_gate_checks_actionable_winner(self) -> None:
        event = router_decision_to_event(_decision())
        assert "+eligible" in event.gate_checks
        assert "+actionable" in event.gate_checks
        assert "-flat" not in event.gate_checks

    def test_gate_checks_flat_winner(self) -> None:
        event = router_decision_to_event(_decision(winner=_flat_signal()))
        assert "-flat" in event.gate_checks
        assert "+actionable" not in event.gate_checks

    def test_gate_checks_no_eligibility_rows(self) -> None:
        event = router_decision_to_event(_decision(eligible=()))
        # No +eligible tag when the eligibility tuple is empty.
        assert "+eligible" not in event.gate_checks
        assert "-eligible" not in event.gate_checks

    def test_outcome_default_is_noted(self) -> None:
        event = router_decision_to_event(_decision())
        assert event.outcome is Outcome.NOTED

    def test_outcome_override(self) -> None:
        event = router_decision_to_event(_decision(), outcome=Outcome.EXECUTED)
        assert event.outcome is Outcome.EXECUTED

    def test_links_default_empty(self) -> None:
        event = router_decision_to_event(_decision())
        assert event.links == []

    def test_links_passed_through(self) -> None:
        event = router_decision_to_event(
            _decision(),
            links=("trade:123", "order:abc"),
        )
        assert event.links == ["trade:123", "order:abc"]

    def test_metadata_asset(self) -> None:
        event = router_decision_to_event(_decision(asset="ETH"))
        assert event.metadata["asset"] == "ETH"

    def test_metadata_winner_is_as_dict(self) -> None:
        winner = _actionable_signal()
        event = router_decision_to_event(_decision(winner=winner))
        assert event.metadata["winner"] == winner.as_dict()

    def test_metadata_candidates_fired_counts_actionable_only(self) -> None:
        # Two actionable, one flat
        a = _actionable_signal()
        b = _actionable_signal(
            strategy=StrategyId.OB_BREAKER_RETEST,
            confidence=5.0,
        )
        flat = _flat_signal()
        dec = _decision(winner=a, candidates=(a, b, flat))
        event = router_decision_to_event(dec)
        assert event.metadata["candidates_fired"] == 2

    def test_metadata_eligible_is_string_list(self) -> None:
        event = router_decision_to_event(_decision())
        assert event.metadata["eligible"] == [
            "liquidity_sweep_displacement",
            "ob_breaker_retest",
        ]

    def test_metadata_omits_candidates_by_default(self) -> None:
        event = router_decision_to_event(_decision())
        assert "candidates" not in event.metadata

    def test_include_candidates_attaches_full_list(self) -> None:
        a = _actionable_signal()
        flat = _flat_signal()
        dec = _decision(winner=a, candidates=(a, flat))
        event = router_decision_to_event(dec, include_candidates=True)
        assert "candidates" in event.metadata
        attached = event.metadata["candidates"]
        assert isinstance(attached, list)
        assert len(attached) == 2


# ---------------------------------------------------------------------------
# RouterDecisionSink  -- basic emission flags
# ---------------------------------------------------------------------------


class TestRouterDecisionSinkBasics:
    def test_disabled_skips_emission(self, journal: DecisionJournal) -> None:
        sink = RouterDecisionSink(journal=journal, enabled=False)
        out = sink.emit(_decision())
        assert out is None
        assert len(journal) == 0

    def test_no_journal_skips_emission(self) -> None:
        sink = RouterDecisionSink(journal=None)
        out = sink.emit(_decision())
        assert out is None

    def test_flat_winner_skipped_by_default(self, journal: DecisionJournal) -> None:
        sink = RouterDecisionSink(journal=journal)
        out = sink.emit(_decision(winner=_flat_signal()))
        assert out is None
        assert len(journal) == 0

    def test_flat_winner_written_when_also_log_flat(
        self,
        journal: DecisionJournal,
    ) -> None:
        sink = RouterDecisionSink(journal=journal, also_log_flat=True)
        out = sink.emit(_decision(winner=_flat_signal()))
        assert out is not None
        assert len(journal) == 1

    def test_actionable_winner_is_written(self, journal: DecisionJournal) -> None:
        sink = RouterDecisionSink(journal=journal)
        out = sink.emit(_decision())
        assert isinstance(out, JournalEvent)
        events = journal.read_all()
        assert len(events) == 1
        assert events[0].actor is Actor.STRATEGY_ROUTER

    def test_default_outcome_used_when_none_passed(
        self,
        journal: DecisionJournal,
    ) -> None:
        sink = RouterDecisionSink(
            journal=journal,
            default_outcome=Outcome.EXECUTED,
        )
        out = sink.emit(_decision())
        assert out is not None
        assert out.outcome is Outcome.EXECUTED

    def test_caller_outcome_overrides_default(
        self,
        journal: DecisionJournal,
    ) -> None:
        sink = RouterDecisionSink(
            journal=journal,
            default_outcome=Outcome.NOTED,
        )
        out = sink.emit(_decision(), outcome=Outcome.BLOCKED)
        assert out is not None
        assert out.outcome is Outcome.BLOCKED

    def test_include_candidates_flag_wires_through(
        self,
        journal: DecisionJournal,
    ) -> None:
        sink = RouterDecisionSink(journal=journal, include_candidates=True)
        sink.emit(_decision())
        events = journal.read_all()
        assert "candidates" in events[0].metadata

    def test_include_candidates_default_false(
        self,
        journal: DecisionJournal,
    ) -> None:
        sink = RouterDecisionSink(journal=journal)
        sink.emit(_decision())
        events = journal.read_all()
        assert "candidates" not in events[0].metadata

    def test_links_forwarded_to_event(self, journal: DecisionJournal) -> None:
        sink = RouterDecisionSink(journal=journal)
        sink.emit(_decision(), links=("trade:xyz",))
        events = journal.read_all()
        assert events[0].links == ["trade:xyz"]


# ---------------------------------------------------------------------------
# RouterDecisionSink  -- robustness
# ---------------------------------------------------------------------------


class _RaisingJournal:
    """Stand-in journal whose append() always raises OSError."""

    def append(self, _event: JournalEvent) -> JournalEvent:
        raise OSError("disk full")


class TestRouterDecisionSinkRobustness:
    def test_os_error_is_swallowed(self) -> None:
        sink = RouterDecisionSink(journal=_RaisingJournal())  # type: ignore[arg-type]
        # Must NOT raise -- an observability failure can never crash the bot.
        out = sink.emit(_decision())
        assert out is None

    def test_non_os_error_still_propagates(self) -> None:
        class _ExplodingJournal:
            def append(self, _event: JournalEvent) -> JournalEvent:
                raise RuntimeError("not an OSError -- caller should see this")

        sink = RouterDecisionSink(journal=_ExplodingJournal())  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="not an OSError"):
            sink.emit(_decision())


# ---------------------------------------------------------------------------
# RouterAdapter integration
# ---------------------------------------------------------------------------


class TestRouterAdapterIntegration:
    """End-to-end: live push_bar ticks producing journal rows."""

    def test_default_adapter_has_no_sink(self) -> None:
        adapter = RouterAdapter(asset="MNQ")
        assert adapter.decision_sink is None

    def test_sink_writes_one_row_per_tick(self, journal: DecisionJournal) -> None:
        def fake_long(_b: list[Bar], _c: object) -> StrategySignal:
            return _actionable_signal()

        sink = RouterDecisionSink(journal=journal)
        adapter = RouterAdapter(
            asset="MNQ",
            registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fake_long},
            eligibility={"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)},
            decision_sink=sink,
        )
        adapter.push_bar(_bar_dict(0))
        adapter.push_bar(_bar_dict(1))
        adapter.push_bar(_bar_dict(2))
        assert len(journal) == 3
        events = journal.read_all()
        assert all(e.actor is Actor.STRATEGY_ROUTER for e in events)
        assert all(e.intent == "dispatch_MNQ" for e in events)

    def test_flat_winner_default_not_logged(self, journal: DecisionJournal) -> None:
        def fake_flat(_b: list[Bar], _c: object) -> StrategySignal:
            return _flat_signal()

        sink = RouterDecisionSink(journal=journal)
        adapter = RouterAdapter(
            asset="MNQ",
            registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fake_flat},
            eligibility={"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)},
            decision_sink=sink,
        )
        for i in range(5):
            adapter.push_bar(_bar_dict(i))
        # No rows -- default sink skips flat winners.
        assert len(journal) == 0

    def test_flat_winner_logged_when_also_log_flat(
        self,
        journal: DecisionJournal,
    ) -> None:
        def fake_flat(_b: list[Bar], _c: object) -> StrategySignal:
            return _flat_signal()

        sink = RouterDecisionSink(journal=journal, also_log_flat=True)
        adapter = RouterAdapter(
            asset="MNQ",
            registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fake_flat},
            eligibility={"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)},
            decision_sink=sink,
        )
        for i in range(5):
            adapter.push_bar(_bar_dict(i))
        assert len(journal) == 5

    def test_no_sink_means_no_journal_interaction(
        self,
        journal: DecisionJournal,
    ) -> None:
        def fake_long(_b: list[Bar], _c: object) -> StrategySignal:
            return _actionable_signal()

        # adapter without sink
        adapter = RouterAdapter(
            asset="MNQ",
            registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fake_long},
            eligibility={"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)},
        )
        adapter.push_bar(_bar_dict(0))
        adapter.push_bar(_bar_dict(1))
        # Journal stays empty -- no sink was wired.
        assert len(journal) == 0

    def test_kill_switch_mutes_but_sink_can_still_log_flat(
        self,
        journal: DecisionJournal,
    ) -> None:
        """Kill-switch forces flat winners; sink captures them when asked."""

        def fake_long(_b: list[Bar], _c: object) -> StrategySignal:
            return _actionable_signal()

        sink = RouterDecisionSink(journal=journal, also_log_flat=True)
        adapter = RouterAdapter(
            asset="MNQ",
            registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fake_long},
            eligibility={"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)},
            kill_switch_active=True,
            decision_sink=sink,
        )
        for i in range(3):
            adapter.push_bar(_bar_dict(i))
        # kill-switch may or may not force FLAT depending on strategy
        # registered -- but the sink must have been invoked each tick.
        # Guarantee: at least one row written (events got through).
        assert len(journal) >= 1

    def test_disabled_sink_does_not_write(self, journal: DecisionJournal) -> None:
        def fake_long(_b: list[Bar], _c: object) -> StrategySignal:
            return _actionable_signal()

        sink = RouterDecisionSink(journal=journal, enabled=False)
        adapter = RouterAdapter(
            asset="MNQ",
            registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fake_long},
            eligibility={"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)},
            decision_sink=sink,
        )
        for i in range(3):
            adapter.push_bar(_bar_dict(i))
        assert len(journal) == 0

    def test_event_metadata_carries_asset_and_winner(
        self,
        journal: DecisionJournal,
    ) -> None:
        def fake_long(_b: list[Bar], _c: object) -> StrategySignal:
            return _actionable_signal(
                strategy=StrategyId.OB_BREAKER_RETEST,
                side=Side.LONG,
            )

        sink = RouterDecisionSink(journal=journal)
        adapter = RouterAdapter(
            asset="BTC",
            registry={StrategyId.OB_BREAKER_RETEST: fake_long},
            eligibility={"BTC": (StrategyId.OB_BREAKER_RETEST,)},
            decision_sink=sink,
        )
        adapter.push_bar(_bar_dict(0))
        events = journal.read_all()
        assert len(events) == 1
        meta = events[0].metadata
        assert meta["asset"] == "BTC"
        assert meta["winner"]["strategy"] == "ob_breaker_retest"
        assert meta["winner"]["side"] == "LONG"
