from __future__ import annotations

from datetime import UTC, datetime

from eta_engine.brain.jarvis_admin import ActionRequest, ActionType, SubsystemId
from eta_engine.brain.jarvis_context import (
    ActionSuggestion,
    EquitySnapshot,
    JarvisContext,
    JarvisSuggestion,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    SessionPhase,
    StressComponent,
    StressScore,
)
from eta_engine.brain.jarvis_v3.unleashed import EtaEngineCore, factory


def _context() -> JarvisContext:
    return JarvisContext(
        ts=datetime(2026, 4, 29, 14, 0, tzinfo=UTC),
        macro=MacroSnapshot(vix_level=18.0, next_event_label="FOMC", hours_until_next_event=2.0),
        equity=EquitySnapshot(
            account_equity=50_000,
            daily_pnl=250,
            daily_drawdown_pct=0.01,
            open_positions=1,
            open_risk_r=0.5,
        ),
        regime=RegimeSnapshot(regime="NEUTRAL", confidence=0.8),
        journal=JournalSnapshot(),
        suggestion=JarvisSuggestion(action=ActionSuggestion.TRADE, reason="edge active", confidence=0.7),
        stress_score=StressScore(
            composite=0.22,
            binding_constraint="macro_event",
            components=[
                StressComponent(name="macro_event", value=0.2, weight=0.25),
                StressComponent(name="equity_dd", value=0.1, weight=0.25),
                StressComponent(name="open_risk", value=0.2, weight=0.15),
            ],
        ),
        session_phase=SessionPhase.OPEN_DRIVE,
    )


def test_unleashed_decide_bootstraps_without_admin_and_applies_doctrine() -> None:
    core = EtaEngineCore()
    request = ActionRequest(
        subsystem=SubsystemId.BOT_MNQ,
        action=ActionType.ORDER_PLACE,
        rationale="unit test trade request",
    )

    decision = core.decide(request, _context(), now=datetime(2026, 4, 29, 14, 0, tzinfo=UTC))

    assert decision.request_id == request.request_id
    assert decision.base_verdict == "APPROVED"
    assert decision.doctrine_verdict == "CONDITIONAL"
    assert decision.final_verdict == "CONDITIONAL"
    assert decision.calibrated is not None
    assert decision.projection is not None
    assert any(note.startswith("regime-reweight") for note in decision.notes)


def test_unleashed_dashboard_snapshot_is_renderable_payload() -> None:
    now = datetime(2026, 4, 29, 14, 0, tzinfo=UTC)
    payload = EtaEngineCore().dashboard_snapshot(_context(), now=now)

    assert payload["ts"] == now.isoformat()
    assert payload["suggestion"] == "TRADE"
    assert payload["stress"]["components"][0]["name"] == "macro_event"  # type: ignore[index]
    assert payload["budget"]["tier_state"] == "OK"  # type: ignore[index]
    assert payload["kaizen"]["severity"] == "RED"  # type: ignore[index]


def test_unleashed_factory_wires_admin_when_audit_path_is_supplied(tmp_path) -> None:
    core = factory(audit_path=tmp_path / "jarvis_audit.jsonl")

    assert isinstance(core, EtaEngineCore)
    assert core.admin is not None
