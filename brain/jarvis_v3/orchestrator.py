"""JarvisOrchestrator (Wave-11, 2026-04-27).

Single-call integration layer that consults every supercharge wave
and emits a unified DecisionPacket the trade engine can consume.

Layers consulted, in order:

  1. RAG enrichment (memory_rag) -- analog episode lookup
  2. Causal evidence (causal_layer) -- post-screening signal vetting
  3. World-model dream (world_model + world_model_full) -- expected
     return + path quantiles for the proposed action
  4. Firm-board iterative debate (firm_board_debate) -- 5-role
     deliberation with cross-critique
  5. Quantum optimizer (quantum_agent) -- only on the daily-rebalance
     path, not the trade-decision hot path

All consultations are best-effort: if any individual layer raises
or returns no useful info, the orchestrator records the gap in the
DecisionPacket and continues. Production runtime gets a
single object to log into the journal.

This is the user-visible "supercharged JARVIS" entry point.
Everything before this was building blocks; this is the assembly.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.firm_board_debate import IterativeVerdict
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.memory_rag import EnrichedContext
    from eta_engine.brain.jarvis_v3.world_model_full import ActionRanking

logger = logging.getLogger(__name__)

POLICY_AUTHORITY = "JARVIS"


def _proposal_audit_context(proposal: Proposal) -> dict[str, Any]:
    """Serializable proposal snapshot for deterministic replay."""
    return {
        "signal_id": proposal.signal_id,
        "direction": proposal.direction,
        "regime": proposal.regime,
        "session": proposal.session,
        "stress": proposal.stress,
        "sentiment": proposal.sentiment,
        "sage_score": proposal.sage_score,
        "slippage_bps_estimate": proposal.slippage_bps_estimate,
        "extra": dict(proposal.extra),
    }


def _stable_decision_seed(proposal: Proposal) -> int:
    payload = json.dumps(
        _proposal_audit_context(proposal),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.blake2s(payload.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=False)


@dataclass
class DecisionPacket:
    """The aggregated output of all consultations -- everything a
    trade engine needs to log + act on."""

    ts: str
    proposal_id: str
    direction: str
    final_action: str
    final_size_multiplier: float  # e.g. 1.0, 0.5, 0.25, 0.0
    confidence: float  # in [0, 1]
    policy_authority: str = POLICY_AUTHORITY
    decision_seed: int | None = None

    # Per-layer outputs
    rag_summary: str = ""
    rag_cautions: list[str] = field(default_factory=list)
    rag_boosts: list[str] = field(default_factory=list)

    causal_score: float = 0.0
    causal_reason: str = ""

    world_model_best_action: str = ""
    world_model_expected_r: float = 0.0
    world_model_pct_paths_profitable: float = 0.0

    firm_board_consensus: float = 0.0
    firm_board_devils_advocate: str | None = None

    quantum_used: bool = False
    quantum_contribution_summary: str = ""

    layer_errors: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_audit_record(self) -> dict:
        return {
            "ts": self.ts,
            "proposal_id": self.proposal_id,
            "direction": self.direction,
            "policy_authority": self.policy_authority,
            "decision_seed": self.decision_seed,
            "final_action": self.final_action,
            "final_size_multiplier": self.final_size_multiplier,
            "confidence": self.confidence,
            "layers": {
                "rag": {
                    "summary": self.rag_summary,
                    "cautions": self.rag_cautions,
                    "boosts": self.rag_boosts,
                },
                "causal": {
                    "score": self.causal_score,
                    "reason": self.causal_reason,
                },
                "world_model": {
                    "best_action": self.world_model_best_action,
                    "expected_r": self.world_model_expected_r,
                    "pct_paths_profitable": self.world_model_pct_paths_profitable,
                },
                "firm_board": {
                    "consensus": self.firm_board_consensus,
                    "devils_advocate": self.firm_board_devils_advocate,
                },
                "quantum": {
                    "used": self.quantum_used,
                    "summary": self.quantum_contribution_summary,
                },
            },
            "layer_errors": self.layer_errors,
            "raw": self.raw,
        }


# ─── Action -> size multiplier ────────────────────────────────────


_ACTION_TO_SIZE = {
    "APPROVE_FULL": 1.0,
    "APPROVE_HALF": 0.5,
    "DEFER": 0.0,
    "DENY": 0.0,
}


# ─── Orchestrator ─────────────────────────────────────────────────


class JarvisOrchestrator:
    """Aggregates every supercharge layer into one DecisionPacket.

    Construction parameters:
      memory:    HierarchicalMemory instance (required for RAG, world-
                 model, firm-board auditor)
      use_iterative_debate: True -> firm_board_debate (3-round);
                 False -> firm_board.deliberate (single-pass, faster)
      consult_quantum: only meaningful on daily-rebalance/regime-change
                 paths; default False because trade decisions can't
                 wait for cloud quantum
      causal_threshold: if causal score is below this, the orchestrator
                 enforces DENY regardless of other layers
    """

    def __init__(
        self,
        *,
        memory: HierarchicalMemory,
        use_iterative_debate: bool = True,
        consult_quantum: bool = False,
        causal_threshold: float = -0.4,
    ) -> None:
        self.memory = memory
        self.use_iterative_debate = use_iterative_debate
        self.consult_quantum = consult_quantum
        self.causal_threshold = causal_threshold

    def deliberate(
        self,
        *,
        proposal: Proposal,
        current_narrative: str = "",
        causal_feature_history: dict[str, list[float]] | None = None,
    ) -> DecisionPacket:
        """Run all layers and assemble the DecisionPacket."""
        layer_errors: list[str] = []
        decision_seed = _stable_decision_seed(proposal)
        raw_context = {
            "policy_authority": POLICY_AUTHORITY,
            "decision_seed": decision_seed,
            "proposal": _proposal_audit_context(proposal),
            "quantum_requested": self.consult_quantum,
            "use_iterative_debate": self.use_iterative_debate,
        }

        # 1. RAG enrichment
        rag_ctx = self._consult_rag(proposal, current_narrative, layer_errors)

        # 2. Causal evidence
        causal_score, causal_reason = self._consult_causal(
            proposal,
            causal_feature_history,
            layer_errors,
        )

        # 3. World-model action ranking
        wm_best_action, wm_expected_r, wm_pct_profit = self._consult_world_model(
            proposal,
            layer_errors,
        )

        # 4. Firm-board debate
        verdict = self._consult_firm_board(
            proposal,
            layer_errors,
            decision_seed=decision_seed,
        )

        # 5. Quantum (optional, daily-rebalance only)
        quantum_summary = ""
        if self.consult_quantum:
            quantum_summary = self._consult_quantum(proposal, layer_errors)

        # ── Synthesis ─────────────────────────────────────────────
        # Causal veto: if score is very negative, DENY regardless
        if causal_score < self.causal_threshold:
            final_action = "DENY"
            final_size = 0.0
            confidence = 1.0
            ts = datetime.now(UTC).isoformat()
            return DecisionPacket(
                ts=ts,
                proposal_id=proposal.signal_id,
                direction=proposal.direction,
                final_action=final_action,
                final_size_multiplier=final_size,
                confidence=confidence,
                decision_seed=decision_seed,
                rag_summary=rag_ctx.summary if rag_ctx else "",
                rag_cautions=list(rag_ctx.cautions) if rag_ctx else [],
                rag_boosts=list(rag_ctx.boosts) if rag_ctx else [],
                causal_score=causal_score,
                causal_reason=causal_reason
                or f"causal score {causal_score:+.2f} < veto threshold {self.causal_threshold:+.2f}",
                world_model_best_action=wm_best_action,
                world_model_expected_r=wm_expected_r,
                world_model_pct_paths_profitable=wm_pct_profit,
                firm_board_consensus=(verdict.round_3_consensus if verdict else 0.0),
                firm_board_devils_advocate=(
                    verdict.devils_advocate_role.value if verdict and verdict.devils_advocate_role else None
                ),
                quantum_used=bool(quantum_summary),
                quantum_contribution_summary=quantum_summary,
                layer_errors=layer_errors,
                raw=raw_context,
            )

        # Otherwise: firm-board's final action drives, modulated by
        # RAG cautions (each caution shrinks size by 25%, capped at 0)
        if verdict is None:
            final_action = "DEFER"
            final_size = 0.0
            confidence = 0.3
        else:
            final_action = verdict.final_action.value
            base_size = _ACTION_TO_SIZE.get(final_action, 0.0)
            n_cautions = len(rag_ctx.cautions) if rag_ctx else 0
            shrink = max(0.0, 1.0 - 0.25 * n_cautions)
            final_size = base_size * shrink
            confidence = round(verdict.round_3_consensus, 3)

        return DecisionPacket(
            ts=datetime.now(UTC).isoformat(),
            proposal_id=proposal.signal_id,
            direction=proposal.direction,
            final_action=final_action,
            final_size_multiplier=round(final_size, 3),
            confidence=confidence,
            decision_seed=decision_seed,
            rag_summary=rag_ctx.summary if rag_ctx else "",
            rag_cautions=list(rag_ctx.cautions) if rag_ctx else [],
            rag_boosts=list(rag_ctx.boosts) if rag_ctx else [],
            causal_score=causal_score,
            causal_reason=causal_reason,
            world_model_best_action=wm_best_action,
            world_model_expected_r=wm_expected_r,
            world_model_pct_paths_profitable=wm_pct_profit,
            firm_board_consensus=(verdict.round_3_consensus if verdict else 0.0),
            firm_board_devils_advocate=(
                verdict.devils_advocate_role.value if verdict and verdict.devils_advocate_role else None
            ),
            quantum_used=bool(quantum_summary),
            quantum_contribution_summary=quantum_summary,
            layer_errors=layer_errors,
            raw=raw_context,
        )

    # ── Per-layer consultations ───────────────────────────────

    def _consult_rag(
        self,
        proposal: Proposal,
        current_narrative: str,
        errors: list[str],
    ) -> EnrichedContext | None:
        try:
            from eta_engine.brain.jarvis_v3.memory_rag import (
                rag_enrich_decision_context,
            )

            return rag_enrich_decision_context(
                current_narrative=current_narrative,
                regime=proposal.regime,
                session=proposal.session,
                stress=proposal.stress,
                direction=proposal.direction,
                memory=self.memory,
                k=5,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"rag: {exc}")
            return None

    def _consult_causal(
        self,
        proposal: Proposal,
        feature_history: dict[str, list[float]] | None,
        errors: list[str],
    ) -> tuple[float, str]:
        try:
            from eta_engine.brain.jarvis_v3.causal_layer import (
                score_causal_support,
            )

            ev = score_causal_support(
                signal_features={
                    "sentiment": proposal.sentiment,
                    "sage_score": proposal.sage_score,
                },
                proposed_action="approve_full",
                regime=proposal.regime,
                session=proposal.session,
                direction=proposal.direction,
                memory=self.memory,
                feature_history=feature_history,
            )
            return ev.score, ev.reason
        except Exception as exc:  # noqa: BLE001
            errors.append(f"causal: {exc}")
            return 0.0, "causal layer unavailable"

    def _consult_world_model(
        self,
        proposal: Proposal,
        errors: list[str],
    ) -> tuple[str, float, float]:
        try:
            from eta_engine.brain.jarvis_v3.world_model import (
                dream,
                encode_state,
            )
            from eta_engine.brain.jarvis_v3.world_model_full import (
                ActionConditionedTable,
                rank_actions,
            )

            s = encode_state(
                regime=proposal.regime,
                session=proposal.session,
                stress=proposal.stress,
            )
            # Action-conditioned ranking (full build)
            table = ActionConditionedTable()
            table.fit_from_episodes(self.memory._episodes)
            ranking: ActionRanking | None = rank_actions(
                state=s,
                table=table,
                n_rollouts=10,
                horizon=4,
            )
            best_action = ranking.best_action() or "approve_full"
            best_value = ranking.ranked[0][1].expected_return if ranking.ranked else 0.0
            # Lean dream rollouts for pct_paths_profitable
            dream_report = dream(
                current_state=s,
                n_paths=20,
                horizon=4,
                memory=self.memory,
            )
            return best_action, best_value, dream_report.pct_paths_profitable
        except Exception as exc:  # noqa: BLE001
            errors.append(f"world_model: {exc}")
            return "approve_full", 0.0, 0.0

    def _consult_firm_board(
        self,
        proposal: Proposal,
        errors: list[str],
        *,
        decision_seed: int | None = None,
    ) -> IterativeVerdict | None:
        try:
            if self.use_iterative_debate:
                from eta_engine.brain.jarvis_v3.firm_board_debate import (
                    deliberate_iterative,
                )

                return deliberate_iterative(
                    proposal=proposal,
                    memory=self.memory,
                    seed=decision_seed,
                )
            from eta_engine.brain.jarvis_v3.firm_board import deliberate

            single_pass = deliberate(proposal=proposal, memory=self.memory)
            # Wrap in iterative-shape adapter
            from eta_engine.brain.jarvis_v3.firm_board_debate import (
                IterativeVerdict,
            )

            return IterativeVerdict(
                ts=single_pass.ts,
                proposal_id=single_pass.proposal_id,
                round_1_arguments=list(single_pass.arguments),
                round_2_rebuttals=[],
                round_3_final_arguments=list(single_pass.arguments),
                round_1_consensus=single_pass.consensus,
                round_3_consensus=single_pass.consensus,
                final_action=single_pass.final_action,
                devils_advocate_role=None,
                reasoning=single_pass.reasoning,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"firm_board: {exc}")
            return None

    def _consult_quantum(
        self,
        proposal: Proposal,
        errors: list[str],
    ) -> str:
        try:
            from eta_engine.brain.jarvis_v3.quantum import (
                QuantumOptimizerAgent,
                SignalScore,
            )

            agent = QuantumOptimizerAgent()
            # Toy basket: just demonstrates the call. Real usage feeds
            # from candidate-signal pool.
            cands = [
                SignalScore(
                    name="primary",
                    score=proposal.sage_score,
                    features=[proposal.sentiment, proposal.stress, 0.0],
                ),
                SignalScore(
                    name="sentiment_only",
                    score=proposal.sentiment,
                    features=[proposal.sentiment, 0.0, 0.0],
                ),
            ]
            rec = agent.select_signal_basket(
                candidates=cands,
                max_picks=1,
                use_qubo=False,
            )
            return rec.contribution_summary
        except Exception as exc:  # noqa: BLE001
            errors.append(f"quantum: {exc}")
            return ""
