"""Pre-mortem analyzer (Wave-13, 2026-04-27).

Standard JARVIS approves trades. The premortem layer asks the
opposite question BEFORE approval lands:

    "If this trade is going to lose, what's the most likely way?"

By explicitly enumerating failure modes WITH probabilities derived
from the world-model transition tensor + journaled analogs, JARVIS
gets a structured kill-prob that:

  * Surfaces blind spots the firm-board missed
  * Gives the operator a tangible "if X happens, exit" pre-commit
  * Feeds invalidation rules into thesis_tracker

Each FailureMode has a label, probability estimate, and a concrete
TRIGGER condition the runtime can check. The combined "kill_prob"
is 1 - P(no-failure) under the union assumption.

Use case (called inside the orchestrator before final approval):

    from eta_engine.brain.jarvis_v3.premortem import run_premortem

    pm = run_premortem(
        proposal=Proposal(...), memory=memory,
    )
    if pm.kill_prob > 0.50:
        # More likely to lose than win in our model -- defer
        ...
    print(pm.top_failure_modes())

Pure stdlib. Uses world_model rollouts + memory analog lookups.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

logger = logging.getLogger(__name__)


@dataclass
class FailureMode:
    """One enumerated way the trade could lose."""

    label: str  # e.g. "regime shift to bearish_high_vol"
    probability: float  # in [0, 1]
    expected_loss_r: float  # signed R if this mode triggers
    trigger_description: str  # operator-readable condition
    source: str  # which layer surfaced this


@dataclass
class PreMortemReport:
    """Structured pre-trade failure analysis."""

    proposal_signal_id: str
    direction: str
    failure_modes: list[FailureMode] = field(default_factory=list)
    kill_prob: float = 0.0  # in [0, 1]
    expected_loss_if_killed_r: float = 0.0
    note: str = ""

    def top_failure_modes(self, k: int = 3) -> list[FailureMode]:
        return sorted(
            self.failure_modes,
            key=lambda f: f.probability * abs(f.expected_loss_r),
            reverse=True,
        )[:k]

    def to_dict(self) -> dict:
        return {
            "proposal_signal_id": self.proposal_signal_id,
            "direction": self.direction,
            "kill_prob": self.kill_prob,
            "expected_loss_if_killed_r": self.expected_loss_if_killed_r,
            "note": self.note,
            "failure_modes": [
                {
                    "label": f.label,
                    "probability": f.probability,
                    "expected_loss_r": f.expected_loss_r,
                    "trigger_description": f.trigger_description,
                    "source": f.source,
                }
                for f in self.failure_modes
            ],
        }


# ─── Failure-mode enumerators ─────────────────────────────────────


def _regime_shift_failures(
    proposal: Proposal,
    memory: HierarchicalMemory | None,
) -> list[FailureMode]:
    """From transition tensor: probability of moving to a regime that
    historically loses for this direction."""
    if memory is None or not memory._episodes:
        return []
    try:
        from eta_engine.brain.jarvis_v3.world_model import (
            TransitionTable,
            encode_state,
        )

        cur_state = encode_state(
            regime=proposal.regime,
            session=proposal.session,
            stress=proposal.stress,
        )
        table = TransitionTable()
        table.fit_from_episodes(memory._episodes)
        outgoing = table.transitions.get(cur_state, {})
        if not outgoing:
            return []
        total = sum(outgoing.values())

        # Per-state historical avg R (in this direction)
        state_r: dict[int, list[float]] = {}
        for ep in memory._episodes:
            if ep.direction != proposal.direction:
                continue
            s = encode_state(
                regime=ep.regime,
                session=ep.session,
                stress=ep.stress,
            )
            state_r.setdefault(s, []).append(ep.realized_r)
    except Exception as exc:  # noqa: BLE001
        logger.warning("premortem: regime-shift enumerator failed (%s)", exc)
        return []

    out: list[FailureMode] = []
    for next_s, count in outgoing.items():
        if next_s == cur_state:
            continue
        prob = count / total
        rs = state_r.get(next_s, [])
        if not rs:
            continue
        avg = sum(rs) / len(rs)
        if avg >= 0:
            continue  # not a failure mode -- this state historically wins
        from eta_engine.brain.jarvis_v3.world_model import describe_state

        out.append(
            FailureMode(
                label=f"regime shift to {describe_state(next_s)}",
                probability=round(prob, 3),
                expected_loss_r=round(avg, 3),
                trigger_description=(f"if (regime, session, stress) transitions out of {describe_state(cur_state)}"),
                source="world_model.transition_tensor",
            )
        )
    return out


def _analog_loser_failures(
    proposal: Proposal,
    memory: HierarchicalMemory | None,
    *,
    severe_loss_threshold: float = -1.5,
) -> list[FailureMode]:
    """From RAG retrieval: episodes structurally similar that lost
    badly. We surface each unique narrative as a failure mode."""
    if memory is None:
        return []
    similar = memory.recall_similar(
        regime=proposal.regime,
        session=proposal.session,
        stress=proposal.stress,
        direction=proposal.direction,
        k=10,
    )
    losers = [e for e in similar if e.realized_r <= severe_loss_threshold]
    if not losers:
        return []
    # Group by narrative theme (first 50 chars) so dupes don't bloat
    seen: set[str] = set()
    out: list[FailureMode] = []
    n_total = len(similar)
    for ep in losers:
        key = ep.narrative[:50] if ep.narrative else ep.signal_id
        if key in seen:
            continue
        seen.add(key)
        # Probability proxy: this category appeared X out of N analogs
        n_matching = sum(1 for e in losers if (e.narrative[:50] if e.narrative else e.signal_id) == key)
        prob = n_matching / max(n_total, 1)
        out.append(
            FailureMode(
                label=f"analog loss: {key[:60]}",
                probability=round(prob, 3),
                expected_loss_r=round(ep.realized_r, 3),
                trigger_description=(f"if setup mirrors past loser ({ep.signal_id})"),
                source="memory_rag.analog_loser",
            )
        )
    return out


def _stress_spike_failure(proposal: Proposal) -> list[FailureMode]:
    """Heuristic: if stress is already > 0.6, a further spike becomes
    the dominant kill mode."""
    if proposal.stress < 0.5:
        return []
    return [
        FailureMode(
            label="stress spike beyond regime tolerance",
            probability=round(min(1.0, (proposal.stress - 0.4) * 1.2), 3),
            expected_loss_r=-1.0,
            trigger_description=(
                f"if stress rises above {min(1.0, proposal.stress + 0.15):.2f} (currently {proposal.stress:.2f})"
            ),
            source="heuristic.stress_threshold",
        )
    ]


def _adverse_news_window(proposal: Proposal) -> list[FailureMode]:
    """Heuristic: low sentiment paired with long bias is a known kill
    pattern; we add a small probability with the sentiment magnitude
    as a multiplier."""
    if proposal.direction != "long" or proposal.sentiment >= 0:
        return []
    p = min(1.0, abs(proposal.sentiment) * 0.5)
    return [
        FailureMode(
            label="adverse-news-window into long",
            probability=round(p, 3),
            expected_loss_r=-0.8,
            trigger_description=("if sentiment slides further negative within the next 1h"),
            source="heuristic.sentiment_news",
        )
    ]


# ─── Aggregation ─────────────────────────────────────────────────


def run_premortem(
    *,
    proposal: Proposal,
    memory: HierarchicalMemory | None = None,
) -> PreMortemReport:
    """Enumerate failure modes and aggregate kill probability.

    Combines outputs from all enumerators. The kill probability is
    1 - prod(1 - p_i) under the (admittedly rough) independence
    assumption. This understates probability for highly-correlated
    failure modes but the bias is conservative for risk decisions
    when we use it as a "this trade has at least X% kill-prob" floor.
    """
    modes: list[FailureMode] = []
    modes.extend(_regime_shift_failures(proposal, memory))
    modes.extend(_analog_loser_failures(proposal, memory))
    modes.extend(_stress_spike_failure(proposal))
    modes.extend(_adverse_news_window(proposal))

    # Kill probability under independence: 1 - prod(1 - p_i)
    if modes:
        prob_no_fail = 1.0
        for m in modes:
            prob_no_fail *= max(0.0, 1.0 - m.probability)
        kill_prob = 1.0 - prob_no_fail
    else:
        kill_prob = 0.0

    # Expected loss if killed -- weighted average across modes
    if modes:
        weight_sum = sum(m.probability for m in modes)
        exp_loss = sum(m.probability * m.expected_loss_r for m in modes) / weight_sum if weight_sum > 0 else 0.0
    else:
        exp_loss = 0.0

    note = ""
    if not modes:
        note = "no failure modes enumerated (insufficient memory data?)"
    elif kill_prob > 0.5:
        note = "kill prob > 50% -- recommend defer or shrink size"

    return PreMortemReport(
        proposal_signal_id=proposal.signal_id,
        direction=proposal.direction,
        failure_modes=modes,
        kill_prob=round(kill_prob, 3),
        expected_loss_if_killed_r=round(exp_loss, 3),
        note=note,
    )
