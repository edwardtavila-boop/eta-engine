"""Full-build firm-board debate (Wave-10 upgrade of Wave-8 #6).

The lean ``firm_board.py`` runs a single round: 5 roles each emit
one Argument, then synthesize. This module adds iterative debate:

  * ROUND 1 -- Roles emit initial arguments (same as lean)
  * ROUND 2 -- Each role sees the others' arguments and emits a
    REBUTTAL. Rebuttals can adjust score (max +/- 0.3 from initial)
    based on what other roles raised
  * ROUND 3 -- Final positions; consensus measured AFTER cross-
    critique has had a chance to converge or sharpen disagreement
  * CROSS-CRITIQUE: each rebuttal explicitly references which role
    it agrees / disagrees with. Audit log captures the dependency
    graph
  * DEVIL'S-ADVOCATE INJECTION: with probability p, one randomly-
    chosen role is forced to argue the contrary position to break
    conformity bias

This is the structure professional trading desks actually use --
positions get adjusted as the debate progresses, and the auditor
records WHO changed WHOM's mind.

Deterministic by default (no LLM). The argument-construction logic
is rule-based, but the structure is correct so an LLM-backed role
is a drop-in upgrade.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from eta_engine.brain.jarvis_v3.firm_board import (
    Argument,
    FinalAction,
    Proposal,
    Role,
    deliberate,
)

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

logger = logging.getLogger(__name__)


@dataclass
class Rebuttal:
    """Round-2 update of a role's stance after seeing the others."""

    role: Role
    references: list[Role] = field(default_factory=list)  # who they responded to
    score_delta: float = 0.0
    reasoning: str = ""


@dataclass
class IterativeVerdict:
    """Three-round debate output with full audit trail."""

    ts: str
    proposal_id: str
    round_1_arguments: list[Argument]
    round_2_rebuttals: list[Rebuttal]
    round_3_final_arguments: list[Argument]
    round_1_consensus: float
    round_3_consensus: float
    final_action: FinalAction
    devils_advocate_role: Role | None
    reasoning: str

    def to_audit_record(self) -> dict:
        return {
            "ts": self.ts,
            "proposal_id": self.proposal_id,
            "round_1_arguments": [
                {
                    "role": a.role.value,
                    "stance": a.stance,
                    "score": a.score,
                    "reasoning": a.reasoning,
                    "concerns": a.concerns,
                }
                for a in self.round_1_arguments
            ],
            "round_2_rebuttals": [
                {
                    "role": r.role.value,
                    "references": [ref.value for ref in r.references],
                    "score_delta": r.score_delta,
                    "reasoning": r.reasoning,
                }
                for r in self.round_2_rebuttals
            ],
            "round_3_final_arguments": [
                {
                    "role": a.role.value,
                    "stance": a.stance,
                    "score": a.score,
                    "reasoning": a.reasoning,
                    "concerns": a.concerns,
                }
                for a in self.round_3_final_arguments
            ],
            "round_1_consensus": self.round_1_consensus,
            "round_3_consensus": self.round_3_consensus,
            "final_action": self.final_action.value,
            "devils_advocate_role": (
                self.devils_advocate_role.value if self.devils_advocate_role is not None else None
            ),
            "reasoning": self.reasoning,
        }


# ─── Cross-critique logic ─────────────────────────────────────────


def _build_rebuttals(
    initial: list[Argument],
    *,
    rng: random.Random,
) -> list[Rebuttal]:
    """For each role, scan the others' arguments and produce a
    rebuttal that nudges the score in response to credible challenges."""
    rebuttals: list[Rebuttal] = []
    by_role = {a.role: a for a in initial}
    for arg in initial:
        delta = 0.0
        refs: list[Role] = []
        notes: list[str] = []

        # If RISK_COMMITTEE opposes, every other role with stance="support"
        # softens by 0.10
        risk_arg = by_role.get(Role.RISK_COMMITTEE)
        if (
            risk_arg is not None
            and risk_arg.stance == "oppose"
            and arg.role != Role.RISK_COMMITTEE
            and arg.stance == "support"
        ):
            delta -= 0.10
            refs.append(Role.RISK_COMMITTEE)
            notes.append("softened in light of risk committee veto")

        # If 3+ roles agree on a stance, the dissenter softens by 0.05
        stance_counts: dict[str, int] = {}
        for a in initial:
            stance_counts[a.stance] = stance_counts.get(a.stance, 0) + 1
        if stance_counts.get(arg.stance, 0) == 1:
            # This role is alone in its stance
            delta -= 0.05
            notes.append("isolated stance; softened toward consensus")

        # AUDITOR carries weight: if AUDITOR supports + role opposes,
        # role softens
        auditor_arg = by_role.get(Role.AUDITOR)
        if (
            auditor_arg is not None
            and auditor_arg.stance == "support"
            and arg.role not in {Role.AUDITOR, Role.RISK_COMMITTEE}
            and arg.stance == "oppose"
        ):
            delta += 0.10
            refs.append(Role.AUDITOR)
            notes.append("auditor reports analog support; softened opposition")

        rebuttals.append(
            Rebuttal(
                role=arg.role,
                references=refs,
                score_delta=round(delta, 3),
                reasoning="; ".join(notes) if notes else "no rebuttal",
            )
        )
    return rebuttals


def _apply_rebuttals(
    initial: list[Argument],
    rebuttals: list[Rebuttal],
) -> list[Argument]:
    """Produce round-3 arguments: initial scores + rebuttal deltas,
    re-categorizing stance based on the new score."""
    delta_by_role = {r.role: r.score_delta for r in rebuttals}
    out: list[Argument] = []
    for arg in initial:
        new_score = max(-1.0, min(1.0, arg.score + delta_by_role.get(arg.role, 0.0)))
        if new_score > 0.15:
            stance = "support"
        elif new_score < -0.15:
            stance = "oppose"
        else:
            stance = "neutral"
        # Append rebuttal note if any
        notes = arg.reasoning
        if delta_by_role.get(arg.role, 0.0) != 0.0:
            notes += f" (post-debate: {delta_by_role[arg.role]:+.2f})"
        out.append(
            Argument(
                role=arg.role,
                stance=stance,
                score=round(new_score, 3),
                reasoning=notes,
                concerns=list(arg.concerns),
            )
        )
    return out


# ─── Devil's advocate ─────────────────────────────────────────────


def _inject_devils_advocate(
    arguments: list[Argument],
    *,
    rng: random.Random,
    probability: float = 0.20,
) -> tuple[list[Argument], Role | None]:
    """With probability ``probability``, flip one role's stance to
    play devil's advocate. Used to break conformity in unanimous
    initial rounds."""
    stance_counts: dict[str, int] = {}
    for a in arguments:
        stance_counts[a.stance] = stance_counts.get(a.stance, 0) + 1
    if max(stance_counts.values()) < len(arguments):
        return arguments, None  # already split, no need
    if rng.random() > probability:
        return arguments, None
    target = rng.choice(arguments)
    flipped: list[Argument] = []
    advocate_role = target.role
    for a in arguments:
        if a.role == advocate_role:
            new_stance = "oppose" if a.stance == "support" else "support" if a.stance == "oppose" else "oppose"
            flipped.append(
                Argument(
                    role=a.role,
                    stance=new_stance,
                    score=-a.score if a.score != 0 else -0.20,
                    reasoning=f"DEVIL'S ADVOCATE: {a.reasoning}",
                    concerns=list(a.concerns),
                )
            )
        else:
            flipped.append(a)
    return flipped, advocate_role


# ─── Consensus + final ────────────────────────────────────────────


def _consensus_score(arguments: list[Argument]) -> float:
    if not arguments:
        return 0.0
    counts: dict[str, int] = {}
    for a in arguments:
        counts[a.stance] = counts.get(a.stance, 0) + 1
    return max(counts.values()) / len(arguments)


def _final_action(arguments: list[Argument]) -> tuple[FinalAction, str]:
    avg = sum(a.score for a in arguments) / len(arguments)
    risk = next((a for a in arguments if a.role == Role.RISK_COMMITTEE), None)
    if risk and risk.stance == "oppose":
        return FinalAction.DENY, f"Risk committee veto: {risk.reasoning}"
    if avg > 0.4:
        return FinalAction.APPROVE_FULL, f"Avg score {avg:+.2f} > 0.4"
    if avg > 0.1:
        return FinalAction.APPROVE_HALF, f"Avg score {avg:+.2f}; partial commit"
    if avg > -0.2:
        return FinalAction.DEFER, f"Avg score {avg:+.2f}; not enough conviction"
    return FinalAction.DENY, f"Avg score {avg:+.2f} < -0.2"


# ─── Public entry point ───────────────────────────────────────────


def deliberate_iterative(
    *,
    proposal: Proposal,
    memory: HierarchicalMemory | None = None,
    devils_advocate_probability: float = 0.20,
    seed: int | None = None,
) -> IterativeVerdict:
    """Three-round debate with cross-critique and optional devil's
    advocate."""
    rng = random.Random(seed) if seed is not None else random.Random()

    # ROUND 1: same as lean
    initial_verdict = deliberate(proposal=proposal, memory=memory)
    round_1_arguments = list(initial_verdict.arguments)

    # Devil's advocate (optional)
    perturbed, devils_role = _inject_devils_advocate(
        round_1_arguments,
        rng=rng,
        probability=devils_advocate_probability,
    )

    # ROUND 2: rebuttals
    rebuttals = _build_rebuttals(perturbed, rng=rng)

    # ROUND 3: final positions
    final_arguments = _apply_rebuttals(perturbed, rebuttals)

    round_1_consensus = _consensus_score(round_1_arguments)
    round_3_consensus = _consensus_score(final_arguments)
    action, reasoning = _final_action(final_arguments)

    return IterativeVerdict(
        ts=datetime.now(UTC).isoformat(),
        proposal_id=proposal.signal_id,
        round_1_arguments=round_1_arguments,
        round_2_rebuttals=rebuttals,
        round_3_final_arguments=final_arguments,
        round_1_consensus=round(round_1_consensus, 3),
        round_3_consensus=round(round_3_consensus, 3),
        final_action=action,
        devils_advocate_role=devils_role,
        reasoning=reasoning,
    )
