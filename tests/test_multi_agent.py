"""Multi-agent orchestrator tests — P10_AI multi_agent_orch."""

from __future__ import annotations

from eta_engine.brain.multi_agent import (
    AgentMessage,
    AgentRole,
    MultiAgentOrchestrator,
)

# ---------------------------------------------------------------------------
# Construction + registration
# ---------------------------------------------------------------------------


def test_empty_orchestrator_returns_hold_consensus() -> None:
    orch = MultiAgentOrchestrator()
    verdict = orch.get_consensus()
    assert verdict["action"] == "HOLD"
    assert verdict["confidence"] == 0.0
    assert verdict["risk_veto"] is False
    assert verdict["signals"] == {}
    assert "No agent signals" in verdict["reasoning"]


def test_register_agent_stores_handler() -> None:
    orch = MultiAgentOrchestrator()
    calls: list[AgentMessage] = []

    def handler(msg: AgentMessage) -> AgentMessage | None:
        calls.append(msg)
        return None

    orch.register_agent(AgentRole.MACRO_GUARDIAN, handler)
    assert AgentRole.MACRO_GUARDIAN in orch._agents


# ---------------------------------------------------------------------------
# broadcast
# ---------------------------------------------------------------------------


def test_broadcast_invokes_all_registered_agents_and_stores_responses() -> None:
    orch = MultiAgentOrchestrator()

    def macro(msg: AgentMessage) -> AgentMessage:
        return AgentMessage(role=AgentRole.MACRO_GUARDIAN, content="calm", priority=6)

    def risk(msg: AgentMessage) -> AgentMessage:
        return AgentMessage(role=AgentRole.RISK_ADVOCATE, content="ok", priority=5)

    orch.register_agent(AgentRole.MACRO_GUARDIAN, macro)
    orch.register_agent(AgentRole.RISK_ADVOCATE, risk)

    trigger = AgentMessage(role=AgentRole.SUPERVISOR, content="TICK")
    resp = orch.broadcast(trigger)

    assert set(resp.keys()) == {AgentRole.MACRO_GUARDIAN, AgentRole.RISK_ADVOCATE}
    assert resp[AgentRole.MACRO_GUARDIAN] is not None
    assert resp[AgentRole.MACRO_GUARDIAN].content == "calm"


def test_broadcast_tolerates_handler_returning_none() -> None:
    orch = MultiAgentOrchestrator()

    def silent(_: AgentMessage) -> None:
        return None

    orch.register_agent(AgentRole.EXECUTION_OPTIMIZER, silent)
    resp = orch.broadcast(AgentMessage(role=AgentRole.SUPERVISOR, content="PING"))
    assert resp[AgentRole.EXECUTION_OPTIMIZER] is None


def test_broadcast_captures_exception_as_error_message() -> None:
    orch = MultiAgentOrchestrator()

    def boom(_: AgentMessage) -> AgentMessage:
        raise RuntimeError("agent down")

    orch.register_agent(AgentRole.STAKING_ALLOCATOR, boom)
    resp = orch.broadcast(AgentMessage(role=AgentRole.SUPERVISOR, content="PING"))
    err = resp[AgentRole.STAKING_ALLOCATOR]
    assert err is not None
    assert "ERROR" in err.content
    assert err.priority == 1


# ---------------------------------------------------------------------------
# get_consensus — action routing
# ---------------------------------------------------------------------------


def _add(orch: MultiAgentOrchestrator, role: AgentRole, content: str, prio: int) -> None:
    def handler(_: AgentMessage) -> AgentMessage:
        return AgentMessage(role=role, content=content, priority=prio)

    orch.register_agent(role, handler)


def test_consensus_trades_on_high_priority_agreement() -> None:
    orch = MultiAgentOrchestrator()
    _add(orch, AgentRole.MACRO_GUARDIAN, "bull macro", prio=8)
    _add(orch, AgentRole.REGIME_DETECTOR, "trending", prio=8)
    _add(orch, AgentRole.RISK_ADVOCATE, "ok", prio=7)
    _add(orch, AgentRole.EXECUTION_OPTIMIZER, "plentiful liq", prio=7)
    _add(orch, AgentRole.STAKING_ALLOCATOR, "idle cash", prio=7)

    orch.broadcast(AgentMessage(role=AgentRole.SUPERVISOR, content="TICK"))
    verdict = orch.get_consensus()

    assert verdict["action"] == "TRADE"
    assert verdict["confidence"] >= 0.7
    assert verdict["risk_veto"] is False
    assert len(verdict["signals"]) == 5


def test_consensus_reduces_on_moderate_priority() -> None:
    orch = MultiAgentOrchestrator()
    _add(orch, AgentRole.MACRO_GUARDIAN, "mixed", prio=5)
    _add(orch, AgentRole.REGIME_DETECTOR, "transition", prio=5)
    _add(orch, AgentRole.RISK_ADVOCATE, "caution", prio=5)

    orch.broadcast(AgentMessage(role=AgentRole.SUPERVISOR, content="TICK"))
    verdict = orch.get_consensus()

    assert verdict["action"] == "REDUCE"
    assert 0.4 <= verdict["confidence"] < 0.7


def test_consensus_holds_on_low_priority() -> None:
    orch = MultiAgentOrchestrator()
    _add(orch, AgentRole.MACRO_GUARDIAN, "unclear", prio=2)
    _add(orch, AgentRole.REGIME_DETECTOR, "noisy", prio=2)

    orch.broadcast(AgentMessage(role=AgentRole.SUPERVISOR, content="TICK"))
    verdict = orch.get_consensus()

    assert verdict["action"] == "HOLD"
    assert verdict["confidence"] < 0.4


def test_risk_advocate_veto_forces_kill() -> None:
    orch = MultiAgentOrchestrator()
    # Everyone else is bullish high-priority — RISK_ADVOCATE still wins.
    _add(orch, AgentRole.MACRO_GUARDIAN, "strong bull", prio=10)
    _add(orch, AgentRole.REGIME_DETECTOR, "clean trend", prio=10)
    _add(orch, AgentRole.RISK_ADVOCATE, "VETO — drawdown breach", prio=10)

    orch.broadcast(AgentMessage(role=AgentRole.SUPERVISOR, content="TICK"))
    verdict = orch.get_consensus()

    assert verdict["action"] == "KILL"
    assert verdict["risk_veto"] is True
    assert "veto" in verdict["reasoning"].lower()


def test_risk_advocate_priority_9_still_triggers_veto() -> None:
    # The code vetoes at priority >= 9, not just == 10.
    orch = MultiAgentOrchestrator()
    _add(orch, AgentRole.RISK_ADVOCATE, "near breach", prio=9)
    orch.broadcast(AgentMessage(role=AgentRole.SUPERVISOR, content="TICK"))
    verdict = orch.get_consensus()
    assert verdict["risk_veto"] is True


def test_risk_advocate_priority_below_9_does_not_veto() -> None:
    orch = MultiAgentOrchestrator()
    _add(orch, AgentRole.RISK_ADVOCATE, "chill", prio=8)
    _add(orch, AgentRole.MACRO_GUARDIAN, "bull", prio=8)
    orch.broadcast(AgentMessage(role=AgentRole.SUPERVISOR, content="TICK"))
    verdict = orch.get_consensus()
    assert verdict["risk_veto"] is False


# ---------------------------------------------------------------------------
# AgentMessage defaults
# ---------------------------------------------------------------------------


def test_agent_message_default_priority_is_five() -> None:
    m = AgentMessage(role=AgentRole.SUPERVISOR, content="hi")
    assert m.priority == 5


def test_agent_message_default_metadata_empty() -> None:
    m = AgentMessage(role=AgentRole.SUPERVISOR, content="hi")
    assert m.metadata == {}
