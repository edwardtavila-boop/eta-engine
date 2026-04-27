"""Multi-agent firm-board debate (Wave-8 #6, 2026-04-27).

Mimics a professional trading desk: instead of one verdict from
JARVIS, five specialist agents independently evaluate a proposal,
critique each other in a debate round, then a Chair synthesizes a
final verdict with a consensus measure.

Roles:

  RESEARCHER -- summarizes raw evidence (sentiment, on-chain, flow)
  STRATEGIST -- maps to theory (Wyckoff, Elliott, regime)
  RISK_COMMITTEE -- challenges every approval; defaults to skepticism
  EXECUTOR -- voices microstructure / fill-quality concerns
  AUDITOR -- compares against historical analogs in memory

The agents are deterministic functions, NOT LLMs. They turn structured
input into structured arguments. This is the SCAFFOLD; LLM-backed
agents are an obvious upgrade path. Even deterministic, the value is:

  * Forces the system to consider 5 perspectives instead of 1
  * Each role has a stable bias (RISK_COMMITTEE always skeptical)
  * Disagreement between roles is a SIGNAL, not noise -- it surfaces
    the actual uncertainty that an averaging model would hide

Use case (advisory mode at first, gating mode later):

    from eta_engine.brain.jarvis_v3.firm_board import deliberate

    verdict = deliberate(
        proposal=Proposal(
            signal_id="cascade_hunter_2026-04-27T15:32",
            direction="long",
            regime="bullish_low_vol",
            session="rth",
            stress=0.3,
            sentiment=0.4,
            sage_score=0.6,
            slippage_bps_estimate=2.1,
        ),
        memory=hierarchical_memory,
    )
    if verdict.consensus < 0.4:
        # Roles disagree sharply -- defer or shrink size
        ...
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

logger = logging.getLogger(__name__)


class Role(StrEnum):
    RESEARCHER = "RESEARCHER"
    STRATEGIST = "STRATEGIST"
    RISK_COMMITTEE = "RISK_COMMITTEE"
    EXECUTOR = "EXECUTOR"
    AUDITOR = "AUDITOR"


class FinalAction(StrEnum):
    APPROVE_FULL = "APPROVE_FULL"
    APPROVE_HALF = "APPROVE_HALF"
    DEFER = "DEFER"
    DENY = "DENY"


@dataclass
class Proposal:
    """Structured input to the firm board."""

    signal_id: str
    direction: str               # "long" or "short"
    regime: str
    session: str
    stress: float
    sentiment: float = 0.0       # in [-1, +1]
    sage_score: float = 0.0      # in [-1, +1]
    slippage_bps_estimate: float = 0.0
    extra: dict = field(default_factory=dict)


@dataclass
class Argument:
    """One role's contribution: stance + score + reasoning."""

    role: Role
    stance: str                  # "support", "neutral", "oppose"
    score: float                 # in [-1, +1]
    reasoning: str
    concerns: list[str] = field(default_factory=list)


@dataclass
class BoardVerdict:
    ts: str
    proposal_id: str
    arguments: list[Argument]
    consensus: float             # in [0, 1]; 1.0 = full agreement
    final_action: FinalAction
    reasoning: str

    def to_audit_record(self) -> dict:
        return {
            "ts": self.ts,
            "proposal_id": self.proposal_id,
            "arguments": [
                {
                    "role": a.role.value,
                    "stance": a.stance,
                    "score": a.score,
                    "reasoning": a.reasoning,
                    "concerns": a.concerns,
                }
                for a in self.arguments
            ],
            "consensus": self.consensus,
            "final_action": self.final_action.value,
            "reasoning": self.reasoning,
        }


# ─── Role implementations ──────────────────────────────────────────


def _researcher(p: Proposal) -> Argument:
    """Summarize the raw evidence layer. Score = sentiment_aligned."""
    aligned = p.sentiment * (1.0 if p.direction == "long" else -1.0)
    if aligned > 0.3:
        return Argument(
            role=Role.RESEARCHER, stance="support", score=aligned,
            reasoning=f"Sentiment {p.sentiment:+.2f} aligns with {p.direction} bias",
        )
    if aligned < -0.3:
        return Argument(
            role=Role.RESEARCHER, stance="oppose", score=aligned,
            reasoning=f"Sentiment {p.sentiment:+.2f} contradicts {p.direction} bias",
            concerns=[f"sentiment misaligned ({aligned:+.2f})"],
        )
    return Argument(
        role=Role.RESEARCHER, stance="neutral", score=aligned,
        reasoning=f"Sentiment near zero ({p.sentiment:+.2f}); inconclusive",
    )


def _strategist(p: Proposal) -> Argument:
    """Theory layer: Sage's score IS the theory verdict."""
    if p.sage_score > 0.4:
        return Argument(
            role=Role.STRATEGIST, stance="support", score=p.sage_score,
            reasoning=f"Sage confluence score {p.sage_score:+.2f} (theory aligned)",
        )
    if p.sage_score < -0.2:
        return Argument(
            role=Role.STRATEGIST, stance="oppose", score=p.sage_score,
            reasoning=f"Sage signals divergence ({p.sage_score:+.2f})",
            concerns=["theory frameworks disagree with this entry"],
        )
    return Argument(
        role=Role.STRATEGIST, stance="neutral", score=p.sage_score,
        reasoning=f"Sage score {p.sage_score:+.2f}; weak theory support",
    )


def _risk_committee(p: Proposal) -> Argument:
    """Stable skeptic. High stress or thin liquidity = oppose."""
    concerns: list[str] = []
    score = 0.0
    if p.stress > 0.7:
        concerns.append(f"stress {p.stress:.2f} > 0.7")
        score -= 0.5
    if p.slippage_bps_estimate > 8:
        concerns.append(f"slippage {p.slippage_bps_estimate:.1f} bps too high")
        score -= 0.3
    if p.session.lower() == "overnight":
        concerns.append("overnight session: thinner book, wider spreads")
        score -= 0.2
    if score < -0.4:
        return Argument(
            role=Role.RISK_COMMITTEE, stance="oppose", score=score,
            reasoning="Risk committee opposes: " + "; ".join(concerns),
            concerns=concerns,
        )
    if score < -0.1:
        return Argument(
            role=Role.RISK_COMMITTEE, stance="neutral", score=score,
            reasoning="Risk committee cautious: " + "; ".join(concerns),
            concerns=concerns,
        )
    return Argument(
        role=Role.RISK_COMMITTEE, stance="support", score=0.1,
        reasoning="No risk concerns at proposed size",
    )


def _executor(p: Proposal) -> Argument:
    """Microstructure focus: slippage + session quality."""
    if p.slippage_bps_estimate > 12:
        return Argument(
            role=Role.EXECUTOR, stance="oppose",
            score=-0.4,
            reasoning=f"Estimated slippage {p.slippage_bps_estimate:.1f} bps "
                      "is too high; consider TWAP",
            concerns=["slippage > 12 bps"],
        )
    if p.slippage_bps_estimate < 3 and p.session.lower() == "rth":
        return Argument(
            role=Role.EXECUTOR, stance="support", score=0.3,
            reasoning="Liquid session, low estimated slippage",
        )
    return Argument(
        role=Role.EXECUTOR, stance="neutral", score=0.0,
        reasoning="Acceptable execution conditions",
    )


def _auditor(p: Proposal, memory: HierarchicalMemory | None) -> Argument:
    """Memory-driven analog comparison. Looks up similar past episodes
    and reports their average R."""
    if memory is None:
        return Argument(
            role=Role.AUDITOR, stance="neutral", score=0.0,
            reasoning="No memory available for analog lookup",
        )
    similar = memory.recall_similar(
        regime=p.regime, session=p.session, stress=p.stress,
        direction=p.direction, k=10,
    )
    if not similar:
        return Argument(
            role=Role.AUDITOR, stance="neutral", score=0.0,
            reasoning="No analogous historical episodes found",
        )
    avg_r = sum(e.realized_r for e in similar) / len(similar)
    win_rate = sum(1 for e in similar if e.realized_r > 0) / len(similar)
    if avg_r > 0.5:
        return Argument(
            role=Role.AUDITOR, stance="support", score=min(1.0, avg_r / 2.0),
            reasoning=f"{len(similar)} analogs avg {avg_r:+.2f}R, "
                      f"wr={win_rate:.0%}",
        )
    if avg_r < -0.3:
        return Argument(
            role=Role.AUDITOR, stance="oppose", score=max(-1.0, avg_r / 2.0),
            reasoning=f"{len(similar)} analogs avg {avg_r:+.2f}R, "
                      f"wr={win_rate:.0%}",
            concerns=[f"historical analogs underperformed (avg {avg_r:+.2f}R)"],
        )
    return Argument(
        role=Role.AUDITOR, stance="neutral", score=avg_r / 2.0,
        reasoning=f"{len(similar)} analogs avg {avg_r:+.2f}R, "
                  f"wr={win_rate:.0%} (mixed)",
    )


# ─── Synthesis ──────────────────────────────────────────────────────


def _consensus(arguments: list[Argument]) -> float:
    """Measure of agreement: 1.0 if all stances match, 0.0 if split."""
    if not arguments:
        return 0.0
    stance_counts: dict[str, int] = {}
    for a in arguments:
        stance_counts[a.stance] = stance_counts.get(a.stance, 0) + 1
    top = max(stance_counts.values())
    return top / len(arguments)


def _final_action(arguments: list[Argument]) -> tuple[FinalAction, str]:
    avg_score = sum(a.score for a in arguments) / len(arguments)
    risk_arg = next((a for a in arguments if a.role == Role.RISK_COMMITTEE), None)

    # Risk committee veto
    if risk_arg and risk_arg.stance == "oppose":
        return FinalAction.DENY, (
            f"Risk committee veto: {risk_arg.reasoning}"
        )

    # Score-based scaling
    if avg_score > 0.4:
        return FinalAction.APPROVE_FULL, f"Avg score {avg_score:+.2f} > 0.4"
    if avg_score > 0.1:
        return FinalAction.APPROVE_HALF, f"Avg score {avg_score:+.2f}, partial commit"
    if avg_score > -0.2:
        return FinalAction.DEFER, f"Avg score {avg_score:+.2f}; not enough conviction"
    return FinalAction.DENY, f"Avg score {avg_score:+.2f} < -0.2"


def deliberate(
    *,
    proposal: Proposal,
    memory: HierarchicalMemory | None = None,
) -> BoardVerdict:
    """Run the full debate. Each role produces an argument, then we
    synthesize a final action with a consensus score."""
    arguments = [
        _researcher(proposal),
        _strategist(proposal),
        _risk_committee(proposal),
        _executor(proposal),
        _auditor(proposal, memory),
    ]
    consensus = _consensus(arguments)
    action, reasoning = _final_action(arguments)
    return BoardVerdict(
        ts=datetime.now(UTC).isoformat(),
        proposal_id=proposal.signal_id,
        arguments=arguments,
        consensus=round(consensus, 3),
        final_action=action,
        reasoning=reasoning,
    )
