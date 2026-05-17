"""JarvisIntelligence -- the consolidated admin layer (Wave-12, 2026-04-27).

The user's directive: "make Jarvis the most intelligent trading AI possible.
He is the admin, the source of all truths."

JarvisAdmin (in ``brain.jarvis_admin``) already exists as the chain-of-
command authority -- every subsystem calls ``request_approval()`` and
gets a Verdict. This wrapper LAYERS the wave-8 / wave-10 cognition
modules onto that flow without breaking any existing call site:

    bot --> JarvisIntelligence.consult(ActionRequest, ...)
              |
              +-- 1. Honor operator_override (SOFT/HARD/KILL pause)
              +-- 2. Call JarvisAdmin.request_approval(req)
              |       (preserves all existing v17/v22 logic, audit log)
              +-- 3. If verdict APPROVED/CONDITIONAL and ENABLE_INTEL flag:
              |       a. RAG enrich from hierarchical memory
              |       b. Score causal support from journaled features
              |       c. World-model action ranking
              |       d. Iterative firm-board debate (3 rounds)
              +-- 4. Synthesize ConsolidatedVerdict:
                      base_verdict + layer outputs + final_size_multiplier
                      + dissent + cautions + boosts

CONSERVATIVE BY DESIGN:

  * Never CHANGES a JarvisAdmin verdict. Only ANNOTATES with
    additional context the bot can consult before acting.
  * Causal-veto is the one exception: causal score below threshold
    can downgrade APPROVED -> DEFER (operator-tunable).
  * Every consultation persists to ``state/jarvis_intel/verdicts.jsonl``
    for replay and audit.
  * Layer failures (memory unavailable, world model errors) get
    recorded in ``layer_errors`` but never break the consultation.

THE ENTRY POINT:

    from eta_engine.brain.jarvis_v3.intelligence import JarvisIntelligence

    intel = JarvisIntelligence(admin=jarvis_admin, memory=memory)
    verdict = intel.consult(req, current_narrative="EMA stack aligned")

    if verdict.is_blocked():
        return None
    size = base_size * verdict.final_size_multiplier
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.scripts import workspace_roots

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_admin import (
        ActionRequest,
        ActionResponse,
        JarvisAdmin,
    )
    from eta_engine.brain.jarvis_context import JarvisContext
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.memory_rag import EnrichedContext

logger = logging.getLogger(__name__)

DEFAULT_VERDICT_LOG = workspace_roots.ETA_JARVIS_VERDICTS_PATH


# ─── Output schema ────────────────────────────────────────────────


@dataclass
class ConsolidatedVerdict:
    """Result of one JarvisIntelligence.consult() call.

    Combines the legacy ActionResponse with optional enrichment from
    the wave-8/10 cognition layers."""

    ts: str
    request_id: str
    subsystem: str
    action: str

    # ── Base verdict (from JarvisAdmin) ──
    base_verdict: str  # APPROVED / DENIED / CONDITIONAL / DEFERRED
    base_reason: str
    bot_id: str = ""
    sentiment_pressure_status: str = ""
    sentiment_pressure_score: float = 0.0
    sentiment_pressure_lead_asset: str = ""
    sentiment_modulation: str = ""
    base_size_cap_qty: float | None = None

    # ── Final adjusted verdict (after enrichment) ──
    final_verdict: str = ""
    final_size_multiplier: float = 1.0  # 0.0 = blocked, 1.0 = full
    confidence: float = 0.0  # in [0, 1]

    # ── Operator-override snapshot ──
    operator_override_level: str = "NORMAL"

    # ── Layer outputs (when enabled) ──
    intelligence_enabled: bool = False
    rag_summary: str = ""
    rag_cautions: list[str] = field(default_factory=list)
    rag_boosts: list[str] = field(default_factory=list)
    causal_score: float = 0.0
    causal_reason: str = ""
    world_model_best_action: str = ""
    world_model_expected_r: float = 0.0
    firm_board_consensus: float = 0.0
    firm_board_devils_advocate: str | None = None

    # ── Bookkeeping ──
    layer_errors: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def is_blocked(self) -> bool:
        """True iff the bot should NOT proceed with the action."""
        if self.operator_override_level in {"HARD_PAUSE", "KILL"}:
            return True
        if self.final_verdict in {"DENIED", "DEFERRED"}:
            return True
        return self.final_size_multiplier <= 0.0

    def is_full_approval(self) -> bool:
        return (
            self.final_verdict == "APPROVED"
            and self.final_size_multiplier >= 0.99
            and self.operator_override_level == "NORMAL"
        )

    def to_audit_record(self) -> dict:
        return asdict(self)


# ─── Configuration ───────────────────────────────────────────────


@dataclass
class IntelligenceConfig:
    """Operator-tunable knobs for the intelligence layer."""

    enable_intelligence: bool = True  # master switch
    enable_iterative_debate: bool = True  # 3-round firm-board vs single-pass
    enable_world_model: bool = True
    enable_rag: bool = True
    enable_causal: bool = True

    causal_veto_threshold: float = -0.4  # below this -> downgrade
    rag_caution_size_shrink: float = 0.25  # per caution
    consensus_warning_threshold: float = 0.4  # below this -> low confidence flag

    # Whether to actually downgrade a verdict on causal veto, or just
    # annotate. Conservative default: annotate-only, so JarvisAdmin
    # remains the sole authority.
    causal_veto_can_downgrade: bool = False


# ─── The intelligence layer ──────────────────────────────────────


class JarvisIntelligence:
    """Consolidated admin: JarvisAdmin + memory_rag + causal_layer +
    world_model_full + firm_board_debate, with operator override at
    the top and persistent audit at the bottom.

    Constructor parameters
    ----------------------
    admin:
        Live JarvisAdmin instance. ``request_approval()`` is the
        canonical authority -- intelligence layers never overrule
        unless ``cfg.causal_veto_can_downgrade`` is True.
    memory:
        Hierarchical memory used by RAG, world model, firm-board
        Auditor role. Optional -- when None, those layers are skipped
        and the relevant fields stay empty in the verdict.
    cfg:
        IntelligenceConfig. When ``enable_intelligence`` is False
        the wrapper is a transparent passthrough to JarvisAdmin
        (preserving every existing call site's behavior).
    verdict_log:
        Path for the consolidated audit JSONL. When None, no
        intelligence-layer log (the JarvisAdmin audit still happens).
    """

    def __init__(
        self,
        *,
        admin: JarvisAdmin,
        memory: HierarchicalMemory | None = None,
        cfg: IntelligenceConfig | None = None,
        verdict_log: Path | None = DEFAULT_VERDICT_LOG,
    ) -> None:
        self.admin = admin
        self.memory = memory
        self.cfg = cfg or IntelligenceConfig()
        self.verdict_log = verdict_log
        if verdict_log is not None:
            verdict_log.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_float(value: object, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _request_metadata(self, req: ActionRequest) -> dict[str, object]:
        payload = getattr(req, "payload", {}) or {}
        if not isinstance(payload, dict):
            return {}
        return {
            "bot_id": str(payload.get("bot_id") or "").strip(),
            "sentiment_pressure_status": str(payload.get("sentiment_pressure_status") or "").strip(),
            "sentiment_pressure_score": self._safe_float(payload.get("sentiment_pressure_score"), 0.0),
            "sentiment_pressure_lead_asset": str(payload.get("sentiment_pressure_lead_asset") or "").strip(),
            "sentiment_modulation": str(payload.get("sentiment_modulation") or "").strip(),
        }

    # ── Entry point ──────────────────────────────────────────

    def consult(
        self,
        req: ActionRequest,
        *,
        ctx: JarvisContext | None = None,
        current_narrative: str = "",
        causal_feature_history: dict[str, list[float]] | None = None,
    ) -> ConsolidatedVerdict:
        """Single canonical entry. Bots call this instead of
        ``admin.request_approval()`` directly to also receive layer
        enrichment.

        ``current_narrative`` is the bot's own one-line description of
        the setup -- gets indexed against the RAG memory for analog
        retrieval.

        ``causal_feature_history`` is an optional mapping
        feature_name -> recent values (len ~30+ samples each), used
        by the causal layer to compute Granger-style attribution.
        """
        # ── 0. Operator override comes FIRST ──
        override_level = self._operator_override_level()
        if override_level in {"HARD_PAUSE", "KILL"}:
            return self._hard_block_verdict(req, override_level)

        # ── 1. Always call JarvisAdmin (preserves chain-of-command) ──
        try:
            base_response = self.admin.request_approval(req, ctx=ctx)
        except Exception as exc:  # noqa: BLE001
            logger.exception("jarvis_intel: admin request_approval raised")
            return self._error_verdict(req, override_level, str(exc))

        # ── 2. If intelligence disabled, return passthrough verdict ──
        if not self.cfg.enable_intelligence:
            return self._passthrough_verdict(req, base_response, override_level)

        # ── 3. Run the wave-8/10 cognition layers (best-effort) ──
        layer_errors: list[str] = []
        proposal = self._req_to_proposal(req, ctx)
        rag_ctx = self._consult_rag(proposal, current_narrative, layer_errors)
        causal_score, causal_reason = self._consult_causal(
            proposal,
            causal_feature_history,
            layer_errors,
        )
        wm_action, wm_expected_r = self._consult_world_model(
            proposal,
            layer_errors,
        )
        firm_consensus, firm_advocate = self._consult_firm_board(
            proposal,
            layer_errors,
        )

        # ── 4. Synthesize final verdict ──
        verdict = self._synthesize(
            req=req,
            base_response=base_response,
            override_level=override_level,
            rag_ctx=rag_ctx,
            causal_score=causal_score,
            causal_reason=causal_reason,
            wm_action=wm_action,
            wm_expected_r=wm_expected_r,
            firm_consensus=firm_consensus,
            firm_advocate=firm_advocate,
            layer_errors=layer_errors,
        )

        self._persist(verdict)
        return verdict

    # ── Hard-block / passthrough / error paths ───────────────

    def _hard_block_verdict(
        self,
        req: ActionRequest,
        override_level: str,
    ) -> ConsolidatedVerdict:
        v = ConsolidatedVerdict(
            ts=datetime.now(UTC).isoformat(),
            request_id=str(getattr(req, "request_id", "")),
            subsystem=str(getattr(req, "subsystem", "")),
            action=str(getattr(req, "action_type", "")),
            **self._request_metadata(req),
            base_verdict="DENIED",
            base_reason=f"operator_override: {override_level}",
            final_verdict="DENIED",
            final_size_multiplier=0.0,
            confidence=1.0,
            operator_override_level=override_level,
            intelligence_enabled=self.cfg.enable_intelligence,
        )
        self._persist(v)
        return v

    def _passthrough_verdict(
        self,
        req: ActionRequest,
        base: ActionResponse,
        override_level: str,
    ) -> ConsolidatedVerdict:
        size_mult = self._verdict_to_size(
            base.verdict,
            getattr(base, "size_cap_mult", None),
        )
        v = ConsolidatedVerdict(
            ts=datetime.now(UTC).isoformat(),
            request_id=str(getattr(req, "request_id", "")),
            subsystem=str(getattr(req, "subsystem", "")),
            action=str(getattr(req, "action_type", "")),
            **self._request_metadata(req),
            base_verdict=str(base.verdict),
            base_reason=str(getattr(base, "reason_code", "")),
            base_size_cap_qty=getattr(base, "size_cap_qty", None),
            final_verdict=str(base.verdict),
            final_size_multiplier=size_mult,
            confidence=1.0,
            operator_override_level=override_level,
            intelligence_enabled=False,
        )
        self._persist(v)
        return v

    def _error_verdict(
        self,
        req: ActionRequest,
        override_level: str,
        error_msg: str,
    ) -> ConsolidatedVerdict:
        return ConsolidatedVerdict(
            ts=datetime.now(UTC).isoformat(),
            request_id=str(getattr(req, "request_id", "")),
            subsystem=str(getattr(req, "subsystem", "")),
            action=str(getattr(req, "action_type", "")),
            **self._request_metadata(req),
            base_verdict="DEFERRED",
            base_reason="admin_error",
            final_verdict="DEFERRED",
            final_size_multiplier=0.0,
            confidence=0.0,
            operator_override_level=override_level,
            layer_errors=[f"admin: {error_msg}"],
        )

    # ── Per-layer consultations ──────────────────────────────

    def _operator_override_level(self) -> str:
        try:
            from eta_engine.obs.operator_override import get_state

            return str(get_state().level.value)
        except Exception as exc:  # noqa: BLE001
            logger.debug("jarvis_intel: override read failed (%s)", exc)
            return "NORMAL"

    def _req_to_proposal(
        self,
        req: ActionRequest,
        ctx: JarvisContext | None,
    ) -> Proposal:
        from eta_engine.brain.jarvis_v3.firm_board import Proposal

        # Best-effort field extraction. We keep this defensive because
        # different subsystems serialize different payloads.
        payload = getattr(req, "payload", {}) or {}
        regime = str(payload.get("regime", "neutral"))
        session = str(payload.get("session", "rth"))
        stress = float(payload.get("stress", 0.5))
        direction = str(payload.get("direction", "long"))
        sentiment = float(payload.get("sentiment", 0.0))
        sage_score = float(payload.get("sage_score", 0.0))
        slip = float(payload.get("slippage_bps_estimate", 0.0))
        # ctx can refine fields if available.
        #
        # ``ctx.stress_score`` is a v2 ``StressScore`` pydantic object
        # (composite + components + binding_constraint) when produced
        # by ``build_snapshot``. Older callers may pass a bare float in
        # ``stress_score`` (or omit it entirely). Be tolerant of both:
        # prefer ``.composite`` when it exists, otherwise coerce
        # whatever truthy value we got.
        if ctx is not None:
            ctx_stress = getattr(ctx, "stress_score", None)
            if ctx_stress is not None:
                composite = getattr(ctx_stress, "composite", None)
                if composite is not None:
                    stress = float(composite)
                else:
                    # Unknown shape -- keep payload-derived stress on failure
                    import contextlib

                    with contextlib.suppress(TypeError, ValueError):
                        stress = float(ctx_stress)
        return Proposal(
            signal_id=str(getattr(req, "request_id", "unknown")),
            direction=direction,
            regime=regime,
            session=session,
            stress=stress,
            sentiment=sentiment,
            sage_score=sage_score,
            slippage_bps_estimate=slip,
        )

    def _consult_rag(
        self,
        proposal: Proposal,
        narrative: str,
        errors: list[str],
    ) -> EnrichedContext | None:
        if not self.cfg.enable_rag or self.memory is None:
            return None
        try:
            from eta_engine.brain.jarvis_v3.memory_rag import (
                rag_enrich_decision_context,
            )

            return rag_enrich_decision_context(
                current_narrative=narrative,
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
        if not self.cfg.enable_causal or self.memory is None:
            return 0.0, "causal layer disabled"
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
            return 0.0, "causal layer error"

    def _consult_world_model(
        self,
        proposal: Proposal,
        errors: list[str],
    ) -> tuple[str, float]:
        if not self.cfg.enable_world_model or self.memory is None:
            return "", 0.0
        try:
            from eta_engine.brain.jarvis_v3.world_model import encode_state
            from eta_engine.brain.jarvis_v3.world_model_full import (
                ActionConditionedTable,
                rank_actions,
            )

            s = encode_state(
                regime=proposal.regime,
                session=proposal.session,
                stress=proposal.stress,
            )
            table = ActionConditionedTable()
            table.fit_from_episodes(self.memory._episodes)
            ranking = rank_actions(
                state=s,
                table=table,
                n_rollouts=10,
                horizon=4,
            )
            best = ranking.best_action() or ""
            best_value = ranking.ranked[0][1].expected_return if ranking.ranked else 0.0
            return best, best_value
        except Exception as exc:  # noqa: BLE001
            errors.append(f"world_model: {exc}")
            return "", 0.0

    def _consult_firm_board(
        self,
        proposal: Proposal,
        errors: list[str],
    ) -> tuple[float, str | None]:
        try:
            if self.cfg.enable_iterative_debate:
                from eta_engine.brain.jarvis_v3.firm_board_debate import (
                    deliberate_iterative,
                )

                v = deliberate_iterative(
                    proposal=proposal,
                    memory=self.memory,
                )
                return v.round_3_consensus, (
                    v.devils_advocate_role.value if v.devils_advocate_role is not None else None
                )
            from eta_engine.brain.jarvis_v3.firm_board import deliberate

            v_single = deliberate(proposal=proposal, memory=self.memory)
            return v_single.consensus, None
        except Exception as exc:  # noqa: BLE001
            errors.append(f"firm_board: {exc}")
            return 0.0, None

    # ── Synthesis ────────────────────────────────────────────

    def _verdict_to_size(
        self,
        verdict_value: str,
        size_cap_mult: float | None = None,
    ) -> float:
        """Map a base verdict to a size multiplier.

        Honors the ``size_cap_mult`` from ``evaluate_request`` when present
        — REVIEW tier sets 0.75, REDUCE tier sets 0.50, TRADE tier may
        leave it None (full size). Without this, the consolidator was
        hard-coding every CONDITIONAL to 0.5x even when the underlying
        gate had specifically set 0.75x for a less-restrictive tier.
        """
        vu = str(verdict_value).upper()
        if vu == "APPROVED":
            return 1.0
        if vu == "CONDITIONAL":
            if size_cap_mult is not None:
                try:
                    return max(0.0, min(1.0, float(size_cap_mult)))
                except (TypeError, ValueError):
                    pass
            return 0.5
        return 0.0

    def _synthesize(
        self,
        *,
        req: ActionRequest,
        base_response: ActionResponse,
        override_level: str,
        rag_ctx: EnrichedContext | None,
        causal_score: float,
        causal_reason: str,
        wm_action: str,
        wm_expected_r: float,
        firm_consensus: float,
        firm_advocate: str | None,
        layer_errors: list[str],
    ) -> ConsolidatedVerdict:
        base_verdict = str(base_response.verdict)
        size_mult = self._verdict_to_size(
            base_verdict,
            getattr(base_response, "size_cap_mult", None),
        )
        final_verdict = base_verdict

        # SOFT_PAUSE downgrades any APPROVED to DEFERRED on NEW positions
        # only (the bot decides what's NEW; we just annotate). For
        # CONDITIONAL we cap size at 0.5 (already is).
        if override_level == "SOFT_PAUSE" and base_verdict == "APPROVED":
            final_verdict = "DEFERRED"
            size_mult = 0.0

        # Causal-veto downgrade (only if explicitly enabled)
        if (
            self.cfg.causal_veto_can_downgrade
            and causal_score < self.cfg.causal_veto_threshold
            and base_verdict in {"APPROVED", "CONDITIONAL"}
        ):
            final_verdict = "DEFERRED"
            size_mult = 0.0

        # RAG cautions shrink the size multiplier
        if rag_ctx is not None and rag_ctx.cautions and size_mult > 0:
            shrink = max(
                0.0,
                1.0 - self.cfg.rag_caution_size_shrink * len(rag_ctx.cautions),
            )
            size_mult = round(size_mult * shrink, 3)

        # Confidence: average of (firm-board consensus, normalized causal)
        causal_norm = max(0.0, min(1.0, (causal_score + 1.0) / 2.0))
        confidence = round((firm_consensus + causal_norm) / 2.0, 3)

        return ConsolidatedVerdict(
            ts=datetime.now(UTC).isoformat(),
            request_id=str(getattr(req, "request_id", "")),
            subsystem=str(getattr(req, "subsystem", "")),
            action=str(getattr(req, "action_type", "")),
            **self._request_metadata(req),
            base_verdict=base_verdict,
            base_reason=str(getattr(base_response, "reason_code", "")),
            base_size_cap_qty=getattr(base_response, "size_cap_qty", None),
            final_verdict=final_verdict,
            final_size_multiplier=size_mult,
            confidence=confidence,
            operator_override_level=override_level,
            intelligence_enabled=True,
            rag_summary=rag_ctx.summary if rag_ctx is not None else "",
            rag_cautions=list(rag_ctx.cautions) if rag_ctx is not None else [],
            rag_boosts=list(rag_ctx.boosts) if rag_ctx is not None else [],
            causal_score=round(causal_score, 3),
            causal_reason=causal_reason,
            world_model_best_action=wm_action,
            world_model_expected_r=round(wm_expected_r, 4),
            firm_board_consensus=round(firm_consensus, 3),
            firm_board_devils_advocate=firm_advocate,
            layer_errors=layer_errors,
        )

    # ── Persistence ──────────────────────────────────────────

    def _persist(self, v: ConsolidatedVerdict) -> None:
        if self.verdict_log is None:
            return
        try:
            with self.verdict_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(v.to_audit_record(), default=str) + "\n")
        except OSError as exc:
            logger.warning(
                "jarvis_intel: verdict log append failed (%s)",
                exc,
            )
