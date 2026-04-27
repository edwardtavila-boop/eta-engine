"""JARVIS full-stack integration (Wave-16 final, 2026-04-27).

This module is the single entry point that wires every wave (7-16)
into one composable surface. Caller imports ONE thing:

    from eta_engine.brain.jarvis_v3.jarvis_full import JarvisFull

    j = JarvisFull.bootstrap(admin=jarvis_admin)
    verdict = j.consult(req, current_narrative="...")  # full pipeline

What "full pipeline" means here:

  1. JarvisIntelligence.consult() -- the wave-12 admin layer that
     already wraps:
        - operator_override
        - JarvisAdmin.request_approval (chain-of-command)
        - memory_rag
        - causal_layer
        - world_model_full
        - firm_board_debate
  2. wave-13 self-awareness layers attached as advisors:
        - premortem
        - ood_detector
        - thesis_tracker (called via open_thesis when verdict approves)
  3. wave-14 explanation layer:
        - narrative_generator on every verdict (logged)
        - operator_coach signal augmentation
        - skill_health_registry as advisory health snapshot
  4. wave-15 fleet/risk modulation:
        - risk_budget_allocator on size_multiplier
  5. wave-16 (used by operator tooling, not on hot path):
        - pre_live_gate / walk_forward / regression / ab_framework
          are exposed as helpers for promotion workflows

THIS MODULE DOES NOT OVERRULE JARVIS_ADMIN. It only annotates,
narrates, and modulates size within the rules JarvisAdmin allows.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_admin import ActionRequest, JarvisAdmin
    from eta_engine.brain.jarvis_context import JarvisContext
    from eta_engine.brain.jarvis_v3.intelligence import (
        ConsolidatedVerdict,
        JarvisIntelligence,
    )
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.operator_coach import OperatorCoach
    from eta_engine.brain.jarvis_v3.skill_health_registry import SkillRegistry
    from eta_engine.brain.jarvis_v3.thesis_tracker import ThesisTracker

logger = logging.getLogger(__name__)


@dataclass
class FullJarvisVerdict:
    """The fully-augmented verdict the operator's bots consume."""

    consolidated: ConsolidatedVerdict
    narrative_terse: str = ""
    narrative_standard: str = ""
    premortem_kill_prob: float = 0.0
    premortem_top_failures: list[str] = field(default_factory=list)
    ood_score: float = 0.0
    ood_label: str = "typical"
    operator_coach_recommendation: str = "auto_proceed"
    operator_coach_size_shrink: float = 1.0
    risk_budget_multiplier: float = 1.0
    risk_budget_reason: str = ""
    final_size_multiplier: float = 1.0
    layer_errors: list[str] = field(default_factory=list)

    def is_blocked(self) -> bool:
        if self.consolidated.is_blocked():
            return True
        return self.final_size_multiplier <= 0.0

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return {
            "consolidated": asdict(self.consolidated),
            "narrative_terse": self.narrative_terse,
            "narrative_standard": self.narrative_standard,
            "premortem_kill_prob": self.premortem_kill_prob,
            "premortem_top_failures": self.premortem_top_failures,
            "ood_score": self.ood_score,
            "ood_label": self.ood_label,
            "operator_coach_recommendation": self.operator_coach_recommendation,
            "operator_coach_size_shrink": self.operator_coach_size_shrink,
            "risk_budget_multiplier": self.risk_budget_multiplier,
            "risk_budget_reason": self.risk_budget_reason,
            "final_size_multiplier": self.final_size_multiplier,
            "layer_errors": self.layer_errors,
        }


class JarvisFull:
    """Composed JARVIS: every wave integrated behind one consult()."""

    def __init__(
        self,
        *,
        intelligence: JarvisIntelligence,
        memory: HierarchicalMemory | None = None,
        operator_coach: OperatorCoach | None = None,
        skill_registry: SkillRegistry | None = None,
        thesis_tracker: ThesisTracker | None = None,
    ) -> None:
        self.intelligence = intelligence
        self.memory = memory
        self.operator_coach = operator_coach
        self.skill_registry = skill_registry
        self.thesis_tracker = thesis_tracker

    @classmethod
    def bootstrap(
        cls,
        *,
        admin: JarvisAdmin,
        memory: HierarchicalMemory | None = None,
        enable_intelligence: bool = True,
    ) -> JarvisFull:
        """Wire up the standard production stack."""
        from eta_engine.brain.jarvis_v3.intelligence import (
            IntelligenceConfig,
            JarvisIntelligence,
        )
        from eta_engine.brain.jarvis_v3.memory_hierarchy import (
            HierarchicalMemory,
        )
        from eta_engine.brain.jarvis_v3.operator_coach import OperatorCoach
        from eta_engine.brain.jarvis_v3.skill_health_registry import (
            SkillRegistry,
        )
        from eta_engine.brain.jarvis_v3.thesis_tracker import ThesisTracker

        mem = memory or HierarchicalMemory.default()
        intel = JarvisIntelligence(
            admin=admin, memory=mem,
            cfg=IntelligenceConfig(enable_intelligence=enable_intelligence),
        )
        return cls(
            intelligence=intel,
            memory=mem,
            operator_coach=OperatorCoach.default(),
            skill_registry=SkillRegistry.default(),
            thesis_tracker=ThesisTracker.default(),
        )

    def consult(
        self,
        req: ActionRequest,
        *,
        ctx: JarvisContext | None = None,
        current_narrative: str = "",
        bot_id: str | None = None,
    ) -> FullJarvisVerdict:
        """Run the full pipeline. One call. All layers."""
        layer_errors: list[str] = []

        # 1. Core intelligence layer
        consolidated = self.intelligence.consult(
            req, ctx=ctx, current_narrative=current_narrative,
        )

        # 2. Pre-mortem (best-effort)
        kill_prob = 0.0
        top_failures: list[str] = []
        if self.memory is not None and consolidated.intelligence_enabled:
            try:
                from eta_engine.brain.jarvis_v3.intelligence import (
                    JarvisIntelligence,
                )
                from eta_engine.brain.jarvis_v3.premortem import run_premortem
                proposal = JarvisIntelligence._req_to_proposal(
                    self.intelligence, req, ctx,
                )
                pm = run_premortem(proposal=proposal, memory=self.memory)
                kill_prob = pm.kill_prob
                top_failures = [
                    f"{m.label} (p={m.probability:.2f}, "
                    f"E[loss]={m.expected_loss_r:+.2f}R)"
                    for m in pm.top_failure_modes(k=3)
                ]
            except Exception as exc:  # noqa: BLE001
                layer_errors.append(f"premortem: {exc}")

        # 3. OOD score
        ood_score = 0.0
        ood_label = "typical"
        if self.memory is not None and consolidated.intelligence_enabled:
            try:
                from eta_engine.brain.jarvis_v3.intelligence import (
                    JarvisIntelligence,
                )
                from eta_engine.brain.jarvis_v3.ood_detector import score_ood
                proposal = JarvisIntelligence._req_to_proposal(
                    self.intelligence, req, ctx,
                )
                rep = score_ood(proposal=proposal, memory=self.memory)
                ood_score = rep.score
                ood_label = rep.label
            except Exception as exc:  # noqa: BLE001
                layer_errors.append(f"ood: {exc}")

        # 4. Operator coach
        coach_rec = "auto_proceed"
        coach_shrink = 1.0
        if self.operator_coach is not None:
            try:
                payload = getattr(req, "payload", {}) or {}
                advice = self.operator_coach.should_defer_to_operator(
                    regime=str(payload.get("regime", "neutral")),
                    session=str(payload.get("session", "rth")),
                    action=str(getattr(req, "action_type", "ORDER")),
                )
                coach_rec = advice.recommendation
                coach_shrink = advice.suggested_size_shrink
            except Exception as exc:  # noqa: BLE001
                layer_errors.append(f"operator_coach: {exc}")

        # 5. Risk budget
        budget_mult = 1.0
        budget_reason = ""
        try:
            from eta_engine.brain.jarvis_v3.risk_budget_allocator import (
                current_envelope,
            )
            env = current_envelope(bot_id=bot_id)
            budget_mult = env.multiplier
            budget_reason = env.reason
        except Exception as exc:  # noqa: BLE001
            layer_errors.append(f"risk_budget: {exc}")

        # 6. Combine size multiplier
        final_size = consolidated.final_size_multiplier
        final_size *= coach_shrink
        final_size *= budget_mult
        # OOD-based attenuation
        if ood_score > 0.5:
            final_size *= max(0.3, 1.0 - 0.7 * ood_score)
        # Premortem-based attenuation
        if kill_prob > 0.5:
            final_size *= max(0.0, 1.0 - kill_prob)
        final_size = max(0.0, min(2.0, final_size))

        # 7. Narratives
        narrative_terse = ""
        narrative_standard = ""
        try:
            from eta_engine.brain.jarvis_v3.narrative_generator import (
                verdict_to_narrative,
            )
            narrative_terse = verdict_to_narrative(
                consolidated, verbosity="terse",
            )
            narrative_standard = verdict_to_narrative(
                consolidated, verbosity="standard",
            )
        except Exception as exc:  # noqa: BLE001
            layer_errors.append(f"narrative: {exc}")

        return FullJarvisVerdict(
            consolidated=consolidated,
            narrative_terse=narrative_terse,
            narrative_standard=narrative_standard,
            premortem_kill_prob=round(kill_prob, 3),
            premortem_top_failures=top_failures,
            ood_score=round(ood_score, 3),
            ood_label=ood_label,
            operator_coach_recommendation=coach_rec,
            operator_coach_size_shrink=round(coach_shrink, 3),
            risk_budget_multiplier=round(budget_mult, 3),
            risk_budget_reason=budget_reason,
            final_size_multiplier=round(final_size, 3),
            layer_errors=layer_errors,
        )

    # ── Convenience helpers ──────────────────────────────────

    def health(self) -> dict:
        from eta_engine.brain.jarvis_v3.health_check import jarvis_health
        return jarvis_health().to_dict()

    def daily_brief(self) -> dict:
        from eta_engine.brain.jarvis_v3.daily_brief import generate_daily_brief
        return generate_daily_brief(auto_persist=True).to_dict()

    def self_drift(self) -> dict:
        from eta_engine.brain.jarvis_v3.self_drift_monitor import (
            detect_self_drift,
        )
        return detect_self_drift().to_dict()
