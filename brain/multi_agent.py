"""
EVOLUTIONARY TRADING ALGO  //  brain.multi_agent
====================================
Multi-agent orchestrator. 6 specialist agents, 1 supervisor verdict.
Adversarial consensus — if they all agree, we might actually be right.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class AgentRole(StrEnum):
    MACRO_GUARDIAN = "MACRO_GUARDIAN"
    REGIME_DETECTOR = "REGIME_DETECTOR"
    RISK_ADVOCATE = "RISK_ADVOCATE"
    EXECUTION_OPTIMIZER = "EXECUTION_OPTIMIZER"
    STAKING_ALLOCATOR = "STAKING_ALLOCATOR"
    SUPERVISOR = "SUPERVISOR"


class AgentMessage(BaseModel):
    """Message from an agent to the orchestrator."""

    role: AgentRole
    content: str
    priority: int = Field(ge=1, le=10, default=5)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

AgentHandler = Callable[[AgentMessage], AgentMessage | None]


class MultiAgentOrchestrator:
    """Orchestrates 6 specialist agents into a consensus decision.

    Flow:
        1. Each agent registered with a handler function
        2. broadcast() sends a message to all agents
        3. get_consensus() collects responses + supervisor aggregation

    The Supervisor has final say. If RISK_ADVOCATE vetoes (priority=10),
    the Supervisor must acknowledge the veto in the consensus output.
    """

    def __init__(self) -> None:
        self._agents: dict[AgentRole, AgentHandler] = {}
        self._inbox: dict[AgentRole, list[AgentMessage]] = defaultdict(list)

    def register_agent(self, role: AgentRole, handler: AgentHandler) -> None:
        """Register an agent's handler function."""
        self._agents[role] = handler

    def broadcast(self, msg: AgentMessage) -> dict[AgentRole, AgentMessage | None]:
        """Broadcast a message to all registered agents, collect responses."""
        responses: dict[AgentRole, AgentMessage | None] = {}
        for role, handler in self._agents.items():
            try:
                resp = handler(msg)
                if resp is not None:
                    self._inbox[role].append(resp)
                responses[role] = resp
            except Exception as exc:
                responses[role] = AgentMessage(
                    role=role,
                    content=f"ERROR: {exc}",
                    priority=1,
                )
        return responses

    def get_consensus(self) -> dict[str, Any]:
        """Aggregate signals from all agents into a supervisor verdict.

        Returns:
            {
                "action": str,          # TRADE / HOLD / REDUCE / KILL
                "confidence": float,    # 0.0 - 1.0
                "risk_veto": bool,      # True if RISK_ADVOCATE vetoed
                "signals": {...},       # per-agent last message
                "reasoning": str,       # supervisor summary
            }
        """
        signals: dict[str, str] = {}
        priorities: list[int] = []
        risk_veto = False

        for role in AgentRole:
            msgs = self._inbox.get(role, [])
            if msgs:
                last = msgs[-1]
                signals[role.value] = last.content
                priorities.append(last.priority)
                if role == AgentRole.RISK_ADVOCATE and last.priority >= 9:
                    risk_veto = True

        if not signals:
            return {
                "action": "HOLD",
                "confidence": 0.0,
                "risk_veto": False,
                "signals": {},
                "reasoning": "No agent signals received",
            }

        avg_priority = sum(priorities) / len(priorities) if priorities else 0
        confidence = min(avg_priority / 10.0, 1.0)

        if risk_veto:
            action = "KILL"
            reasoning = "RISK_ADVOCATE veto — halting all activity"
        elif confidence >= 0.7:
            action = "TRADE"
            reasoning = f"High consensus ({confidence:.0%}) across {len(signals)} agents"
        elif confidence >= 0.4:
            action = "REDUCE"
            reasoning = f"Moderate consensus ({confidence:.0%}) — reducing exposure"
        else:
            action = "HOLD"
            reasoning = f"Low consensus ({confidence:.0%}) — standing aside"

        return {
            "action": action,
            "confidence": round(confidence, 4),
            "risk_veto": risk_veto,
            "signals": signals,
            "reasoning": reasoning,
        }
