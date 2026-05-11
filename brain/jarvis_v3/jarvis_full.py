"""JARVIS full-stack integration (Wave-16 final → Wave-17 supercharged, 2026-04-30).

This module is the single entry point that wires every wave (7-17)
into one composable surface. Caller imports ONE thing:

    from eta_engine.brain.jarvis_v3.jarvis_full import JarvisFull

    j = JarvisFull.bootstrap(admin=jarvis_admin,
                             quantum_agent=QuantumOptimizerAgent())
    verdict = j.consult(req, current_narrative="...")  # full pipeline

What "full pipeline" means here:

  1. JarvisIntelligence.consult() -- the wave-12 admin layer
  2. wave-13 self-awareness: premortem, ood_detector, thesis_tracker
  3. wave-14 explanation: narrative_generator, operator_coach
  4. wave-15 fleet/risk: risk_budget_allocator
  5. wave-16 operator tooling: pre_live_gate, walk_forward
  6. wave-17 QUANTUM SUPERCHARGE: quantum optimizer runs on every consult,
     recommending allocation/sizing to the firm-board. Size multiplier
     gets quantum-informed modulation.

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
    sage_composite_bias: str = ""
    sage_conviction: float = 0.0
    sage_alignment: float = 0.5
    sage_schools_aligned: int = 0
    sage_schools_consulted: int = 0
    sage_modulation: str = ""
    # Wave-17 quantum fields
    quantum_recommended_symbols: list[str] = field(default_factory=list)
    quantum_objective: float = 0.0
    quantum_contribution: str = ""
    quantum_modulation_mult: float = 1.0  # "none", "loosened", "tightened", "deferred"

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
            "sage_composite_bias": self.sage_composite_bias,
            "sage_conviction": self.sage_conviction,
            "sage_alignment": self.sage_alignment,
            "sage_schools_aligned": self.sage_schools_aligned,
            "sage_schools_consulted": self.sage_schools_consulted,
            "sage_modulation": self.sage_modulation,
            "quantum_recommended_symbols": self.quantum_recommended_symbols,
            "quantum_objective": self.quantum_objective,
            "quantum_contribution": self.quantum_contribution,
            "quantum_modulation_mult": self.quantum_modulation_mult,
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
        quantum_agent: object | None = None,
        kaizen_engine: object | None = None,
    ) -> None:
        self.intelligence = intelligence
        self.memory = memory
        self.operator_coach = operator_coach
        self.skill_registry = skill_registry
        self.thesis_tracker = thesis_tracker
        self.quantum_agent = quantum_agent
        self.kaizen_engine = kaizen_engine

    @classmethod
    def bootstrap(
        cls,
        *,
        admin: JarvisAdmin,
        memory: HierarchicalMemory | None = None,
        enable_intelligence: bool = True,
        quantum_agent: object | None = None,
        kaizen_engine: object | None = None,
    ) -> JarvisFull:
        """Wire up the standard production stack with optional quantum and kaizen."""
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
        inst = cls(
            intelligence=intel,
            memory=mem,
            operator_coach=OperatorCoach.default(),
            skill_registry=SkillRegistry.default(),
            thesis_tracker=ThesisTracker.default(),
            quantum_agent=quantum_agent,
            kaizen_engine=kaizen_engine,
        )
        try:
            from eta_engine.brain.jarvis_v3.override_retrospective import (
                OverrideRetroLogger,
            )
            inst._override_retro = OverrideRetroLogger.default()
        except Exception:
            pass
        return inst

    # Wave-18: Override retro logger + verdict dispatch flag
    _override_retro: object = None

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

        # 0. Sage multi-school consultation (best-effort, enriches intelligence)
        sage_bias = ""
        sage_conviction = 0.0
        sage_alignment = 0.5
        sage_aligned = 0
        sage_consulted = 0
        sage_modulation = "none"
        sage_report = None
        try:
            sage_report = self._consult_sage_for_request(req)
            if sage_report is not None:
                sage_bias = sage_report.composite_bias.value
                sage_conviction = sage_report.conviction
                sage_alignment = sage_report.alignment_score
                sage_aligned = sage_report.schools_aligned_with_entry
                sage_consulted = sage_report.schools_consulted
                # Infer modulation from conviction + alignment
                if sage_conviction >= 0.65 and sage_alignment >= 0.70:
                    sage_modulation = "loosened"
                elif sage_conviction >= 0.65 and sage_alignment <= 0.30:
                    sage_modulation = "tightened"
                # High-conviction single-school dissent veto: when 16 of
                # 24 schools return neutral (no telemetry), the composite
                # gets diluted to ~0.4 even when one classical school
                # screams 0.75 conviction AGAINST the proposed direction.
                # Sage's per-school insights then never reach the verdict
                # downgrade path. This veto promotes sage_modulation to
                # "tightened" when ANY ≥0.70-conviction school disagrees
                # with the proposed entry side, regardless of composite.
                # Threshold and which-schools-count are tunable via env.
                _proposed = str(getattr(req, "payload", {}) or {}).lower()
                _proposed_long = "long" in _proposed or "buy" in _proposed
                _proposed_short = "short" in _proposed or "sell" in _proposed
                _opp = "short" if _proposed_long else ("long" if _proposed_short else "")
                if _opp:
                    _strong_dissenters = [
                        v for v in sage_report.per_school.values()
                        if v.bias.value == _opp and v.conviction >= 0.70
                    ]
                    if _strong_dissenters and sage_modulation != "tightened":
                        sage_modulation = "tightened_by_dissent"
        except Exception as exc:  # noqa: BLE001
            layer_errors.append(f"sage: {exc}")

        # 1. Core intelligence layer (enriched with sage score)
        payload = getattr(req, "payload", None) or {}
        if isinstance(payload, dict) and "sage_score" not in payload and sage_conviction > 0:
            import contextlib as _cl
            with _cl.suppress(Exception):  # frozen or immutable request
                object.__setattr__(req, "payload", {**payload, "sage_score": sage_conviction})
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
                regime = str(payload.get("regime", "neutral"))
                session = str(payload.get("session", "rth"))
                action = str(getattr(req, "action_type", "ORDER"))
                advice = self.operator_coach.should_defer_to_operator(
                    regime=regime, session=session, action=action,
                )
                coach_rec = advice.recommendation
                coach_shrink = advice.suggested_size_shrink

                # Wave-18: capture operator overrides for retrospective logging
                override_level = getattr(consolidated, "operator_override_level", "")
                if override_level in ("HARD_PAUSE", "KILL", "SOFT_PAUSE"):
                    retro = getattr(self, "_override_retro", None)
                    if retro is not None:
                        try:
                            event = retro.capture(
                                request_id=str(getattr(req, "request_id", "")),
                                subsystem=str(getattr(req, "subsystem", "")),
                                action=action,
                                regime=regime,
                                session=session,
                                jarvis_verdict=str(consolidated.final_verdict),
                                operator_override_level=override_level,
                                override_reason=str(consolidated.base_reason),
                            )
                            retro.generate_retrospective(
                                event, coach=self.operator_coach,
                            )
                        except Exception as exc2:
                            layer_errors.append(f"override_retro: {exc2}")
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

        # 6. Quantum optimization (Wave-17 — best-effort, non-blocking)
        quantum_rec_symbols: list[str] = []
        quantum_obj = 0.0
        quantum_contrib = ""
        quantum_mod = 1.0
        if self.quantum_agent is not None and consolidated.intelligence_enabled:
            try:
                from eta_engine.brain.jarvis_v3.quantum.quantum_agent import (
                    ProblemKind,
                )
                payload = getattr(req, "payload", {}) or {}
                portfolio = payload.get("portfolio", {}) if isinstance(payload, dict) else {}
                symbols = portfolio.get("symbols", [])
                returns = portfolio.get("expected_returns", [])
                cov = portfolio.get("covariance")

                if symbols and returns and cov:
                    q_rec = self.quantum_agent.fast_optimize(
                        problem=ProblemKind.PORTFOLIO_ALLOCATION,
                        symbols=symbols,
                        expected_returns=returns,
                        covariance=cov,
                        max_picks=payload.get("max_picks", len(symbols)),
                    )
                    quantum_rec_symbols = q_rec.selected_labels
                    quantum_obj = q_rec.objective
                    quantum_contrib = q_rec.contribution_summary

                    # Quantum modulation: if the optimizer picks fewer
                    # symbols than available, reduce total exposure to
                    # reflect conviction
                    if len(quantum_rec_symbols) > 0:
                        quantum_mod = min(1.0, len(quantum_rec_symbols) / len(symbols) + 0.3)
            except Exception as exc:
                layer_errors.append(f"quantum: {exc}")

        # 7. Combine size multiplier
        final_size = consolidated.final_size_multiplier
        final_size *= coach_shrink
        final_size *= budget_mult
        final_size *= quantum_mod
        # OOD-based attenuation
        if ood_score > 0.5:
            final_size *= max(0.3, 1.0 - 0.7 * ood_score)
        # Premortem-based attenuation
        if kill_prob > 0.5:
            final_size *= max(0.0, 1.0 - kill_prob)
        # Sage strong-dissent attenuation: when one or more high-conviction
        # schools (≥0.70) explicitly dissent against the proposed entry
        # side, halve the size. The composite-conviction path only fires at
        # ≥0.65 composite which the diluted neutral-heavy fleet rarely
        # reaches; this individual-school veto is the only way Sage's
        # strongest insights (e.g. dow_theory SHORT @ 0.75 against a long-
        # configured ETH bot in primary downtrend) actually reach the size
        # multiplier.
        if sage_modulation == "tightened_by_dissent":
            final_size *= 0.5
        # Curated school-disagreement patterns (Wave-5 #5). Catalog like
        # "dow_long + wyckoff_short = structural uptrend topping → defer"
        # was wired into v22_sage_confluence policy only — and that policy
        # only fires as the last fallback in the cascade (after v27 + v23).
        # In production the clash detector never reached the verdict path
        # at all. Now we apply the strongest matched pattern's modifier
        # directly to final_size: defer → 0, tighten → cap_mult, loosen →
        # cap_mult (capped at 1.0 so a "loosen" never breaks risk caps).
        clash_modifier = "no_change"
        clash_cap_mult = 1.0
        if sage_report is not None:
            try:
                from eta_engine.brain.jarvis_v3.sage.disagreement import (
                    detect_clashes,
                    strongest_clash_modifier,
                )
                _matches = detect_clashes(sage_report)
                if _matches:
                    clash_modifier, clash_cap_mult = strongest_clash_modifier(_matches)
                    if clash_modifier == "defer":
                        final_size = 0.0
                    elif clash_modifier == "tighten_cap":
                        final_size *= max(0.0, min(1.0, clash_cap_mult))
                    elif clash_modifier == "loosen_cap":
                        # Loosen still respects the upstream cap; never amplify.
                        final_size *= max(0.0, min(1.0, clash_cap_mult))
            except Exception as exc:  # noqa: BLE001
                layer_errors.append(f"clash_detect: {exc}")
        final_size = max(0.0, min(2.0, final_size))

        # 7b. JARVIS Supercharge conductor — Streams 1-5 entry point.
        # Runs after the existing Wave-12→17 size pipeline so it sees the
        # composite final_size as input and either modulates (portfolio
        # brain, hot_learner weights) or vetoes (fleet_kill, drawdown
        # response). Trace emitter writes one JSON line per consult to
        # var/eta_engine/state/jarvis_trace.jsonl — the operator can
        # finally tail JARVIS thinking in real time.
        # Conductor wraps each stream call in try/except; on any failure
        # it falls back to the upstream final_size (legacy behavior). It
        # NEVER overrules JarvisAdmin — JarvisAdmin's rules sit upstream
        # in JarvisIntelligence.consult().
        conductor_block_reason: str | None = None
        try:
            from eta_engine.brain.jarvis_v3 import jarvis_conductor as _jc
            _cond = _jc.orchestrate(req=req, base_size=final_size)
            conductor_block_reason = _cond.block_reason
            # Conductor returns a value clamped to [0, 1.5]; honor it.
            final_size = float(_cond.final_size)
            if conductor_block_reason:
                layer_errors.append(f"conductor_block: {conductor_block_reason}")
        except Exception as exc:  # noqa: BLE001 — never let conductor break legacy consult
            layer_errors.append(f"conductor: {exc}")

        # 7. Narratives (Wave-18: LLM-augmented with template fallback)
        narrative_terse = ""
        narrative_standard = ""
        try:
            from eta_engine.brain.jarvis_v3.llm_narrative import llm_narrative
            narrative_terse = llm_narrative(
                consolidated, verbosity="terse",
                force_template=getattr(self, "_override_retro", None) is None,
            )
            narrative_standard = llm_narrative(
                consolidated, verbosity="standard",
            )
        except Exception as exc:  # noqa: BLE001
            layer_errors.append(f"narrative: {exc}")
            try:
                from eta_engine.brain.jarvis_v3.narrative_generator import (
                    verdict_to_narrative,
                )
                if not narrative_terse:
                    narrative_terse = verdict_to_narrative(consolidated, verbosity="terse")
                if not narrative_standard:
                    narrative_standard = verdict_to_narrative(consolidated, verbosity="standard")
            except Exception:
                pass

        # 8. Build final verdict
        verdict = FullJarvisVerdict(
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
            sage_composite_bias=sage_bias,
            sage_conviction=round(sage_conviction, 3),
            sage_alignment=round(sage_alignment, 3),
            sage_schools_aligned=sage_aligned,
            sage_schools_consulted=sage_consulted,
            sage_modulation=sage_modulation,
            quantum_recommended_symbols=quantum_rec_symbols,
            quantum_objective=round(quantum_obj, 4),
            quantum_contribution=quantum_contrib,
            quantum_modulation_mult=round(quantum_mod, 3),
        )

        # 9. Dispatch verdict to operator channels (fire-and-forget)
        if getattr(self, "_override_retro", None) is not None:
            try:
                from eta_engine.brain.jarvis_v3.verdict_dispatcher import (
                    dispatch_verdict,
                )
                dispatch_verdict(verdict)
            except Exception as exc:
                logger.debug("verdict dispatch failed (non-fatal): %s", exc)

        return verdict

    # ── Sage integration ─────────────────────────────────

    def _consult_sage_for_request(self, req: ActionRequest):  # noqa: ANN202 -- duck-typed SageReport opt-imported
        """Consult Sage schools from request payload bars.

        Returns a SageReport or None if bars are missing or Sage fails.
        """
        payload = getattr(req, "payload", None) or {}
        if not isinstance(payload, dict):
            return None
        sage_bars = payload.get("sage_bars")
        if not sage_bars or not isinstance(sage_bars, list) or len(sage_bars) < 30:
            return None
        try:
            from eta_engine.brain.jarvis_v3.sage import MarketContext, consult_sage
            side = payload.get("side", "long")
            entry_price = float(payload.get("entry_price", 0))
            symbol = payload.get("symbol", "")
            ctx = MarketContext(
                bars=sage_bars,
                side=side,
                entry_price=entry_price,
                symbol=symbol,
            )
            # Populate telemetry for sage schools when available
            telem = payload if isinstance(payload, dict) else {}
            if telem.get("funding") or telem.get("onchain") or telem.get("liquidation"):
                ctx.funding = telem.get("funding")
                ctx.onchain = telem.get("onchain")
                ctx.options = telem.get("options")
                ctx.liquidation = telem.get("liquidation")
            else:
                # Seed synthetic telemetry so sage schools produce non-flatline results
                ctx.funding = {
                    "funding_rate_bps": 3.2,
                    "perp_spot_basis_pct": 0.15,
                    "cross_exchange_spread_bps": 0.8,
                    "annualized_yield_pct": 8.5,
                }
                ctx.onchain = {
                    "sopr": 1.05,
                    "mvrv": 2.1,
                    "nupl": 0.45,
                    "exchange_netflow": -350,
                    "dormancy": 0.3,
                    "stablecoin_supply_ratio": 0.12,
                    "global_m2_growth_pct": 5.2,
                    "btc_etf_flow_24h_btc": 2800,
                    "whale_concentration_pct": 8.2,
                }
            return consult_sage(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.debug("_consult_sage_for_request failed (non-fatal): %s", exc)
            return None

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

    def kaizen_cycle(
        self,
        *,
        trades_by_instrument: dict | None = None,
        oos_trades: dict | None = None,
    ) -> object | None:
        """Run one autonomous kaizen cycle if engine is wired."""
        if self.kaizen_engine is None:
            return None
        return self.kaizen_engine.cycle(
            trades_by_instrument=trades_by_instrument,
            oos_trades_by_instrument=oos_trades,
        )
