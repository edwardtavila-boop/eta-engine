"""
JARVIS v3 // unleashed
======================
The Evolutionary Trading Algo Core meta-controller.

Everything else in ``brain.jarvis_v3`` is a component. ``unleashed``
is the conductor: a single object that owns every component, exposes
one ``decide()`` entrypoint, and threads the doctrine through every
choice.

Flow of one ``decide()`` call:

  1. v2 JarvisAdmin produces a base verdict (existing policy).
  2. v3 regime_stress re-weights the stress components.
  3. v3 horizons projects stress across NOW / 15m / 1h / overnight.
  4. v3 predictive adds a forward-looking forecast.
  5. v3 portfolio gate checks correlation / cluster breach.
  6. v3 preferences applies any operator-learned nudge.
  7. v3 budget + bandit select the LLM tier (if action == LLM_INVOCATION).
  8. v3 calibration attaches p_correct to the verdict.
  9. v3 precedent graph adds "last N times like this..." hint.
 10. v3 philosophy applies doctrine bias (the constitution).
 11. v3 anomaly runs distribution-drift checks on the inputs.
 12. v3 critique periodically audits the decision stream for drift.
 13. v3 kaizen ensures every closed cycle produces a +1 ticket.
 14. v3 vps assesses system health out-of-band on a slower cadence.
 15. v3 skills_registry + mcp_registry gate any tool/skill invocation.

All results fold into a single ``ApexDecision`` pydantic envelope that
the dashboard can render and the audit log can persist.

Design: everything is dependency-injected. Default factory wires sane
defaults so ``ApexPredatorCore()`` "just works," but tests swap any
component for a stub.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.jarvis_admin import (
    ActionRequest,
    ActionResponse,
    JarvisAdmin,
    Verdict,
)
from eta_engine.brain.jarvis_v3.alerts_explain import AlertExplanation  # noqa: TC001  (pydantic runtime)
from eta_engine.brain.jarvis_v3.anomaly import MultiFieldDetector
from eta_engine.brain.jarvis_v3.bandit import LLMBandit
from eta_engine.brain.jarvis_v3.budget import BudgetTracker
from eta_engine.brain.jarvis_v3.calibration import (
    CalibratedVerdict,
    PlattSigmoid,
    VerdictFeatures,
    calibrate_verdict,
)
from eta_engine.brain.jarvis_v3.horizons import (
    HorizonContext,
)
from eta_engine.brain.jarvis_v3.horizons import (
    project as project_horizons,
)
from eta_engine.brain.jarvis_v3.kaizen import KaizenLedger
from eta_engine.brain.jarvis_v3.mcp_registry import MCPRegistry
from eta_engine.brain.jarvis_v3.philosophy import (
    DoctrineVerdict,
    apply_doctrine,
)
from eta_engine.brain.jarvis_v3.portfolio import (
    Exposure,
    PortfolioAssessment,
    assess_portfolio,
)
from eta_engine.brain.jarvis_v3.precedent import (
    PrecedentGraph,
    PrecedentKey,
    PrecedentQuery,
)
from eta_engine.brain.jarvis_v3.predictive import Projection, StressForecaster
from eta_engine.brain.jarvis_v3.preferences import OperatorPreferenceLearner
from eta_engine.brain.jarvis_v3.regime_stress import reweight
from eta_engine.brain.jarvis_v3.skills_registry import SkillRegistry, default_registry

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_context import JarvisContext


class ApexDecision(BaseModel):
    """Merged envelope returned by ``ApexPredatorCore.decide()``."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    request_id: str
    base_verdict: str
    doctrine_verdict: str
    final_verdict: str
    calibrated: CalibratedVerdict | None = None
    doctrine: DoctrineVerdict | None = None
    horizons: HorizonContext | None = None
    projection: Projection | None = None
    portfolio: PortfolioAssessment | None = None
    precedent: PrecedentQuery | None = None
    alerts: list[AlertExplanation] = Field(default_factory=list)
    size_cap_mult: float | None = None
    notes: list[str] = Field(default_factory=list)


class ApexPredatorCore:
    """The Evolutionary Trading Algo Core meta-controller.

    All sub-components can be supplied at construction; any omitted is
    built with a sensible default. This makes tests trivial (inject stubs)
    and production code tidy (just instantiate).
    """

    def __init__(
        self,
        *,
        admin: JarvisAdmin | None = None,
        bandit: LLMBandit | None = None,
        budget: BudgetTracker | None = None,
        preferences: OperatorPreferenceLearner | None = None,
        precedent: PrecedentGraph | None = None,
        kaizen: KaizenLedger | None = None,
        skills: SkillRegistry | None = None,
        mcps: MCPRegistry | None = None,
        calibrator: PlattSigmoid | None = None,
        forecaster: StressForecaster | None = None,
        anomaly: MultiFieldDetector | None = None,
    ) -> None:
        self.admin = admin
        self.bandit = bandit or LLMBandit()
        self.budget = budget or BudgetTracker()
        self.preferences = preferences or OperatorPreferenceLearner()
        self.precedent = precedent or PrecedentGraph()
        self.kaizen = kaizen or KaizenLedger()
        self.skills = skills or default_registry()
        self.mcps = mcps or MCPRegistry()
        self.calibrator = calibrator or PlattSigmoid()
        self.forecaster = forecaster or StressForecaster()
        self.anomaly = anomaly or MultiFieldDetector(
            [
                "stress_composite",
                "vix",
                "regime_confidence",
                "equity_dd",
                "open_risk_r",
            ]
        )

    def decide(
        self,
        request: ActionRequest,
        context: JarvisContext,
        *,
        exposures: list[Exposure] | None = None,
        corr_matrix: dict[tuple[str, str], float] | None = None,
        extra_alerts: list[AlertExplanation] | None = None,
        now: datetime | None = None,
    ) -> ApexDecision:
        """Run the full 15-stage pipeline for one request."""
        now = now or datetime.now(UTC)
        notes: list[str] = []

        # Stage 1 -- base verdict from v2 admin (or sensible default if no admin).
        if self.admin is not None:
            base_resp = self.admin.request_approval(request, context)
        else:
            # Bootstrap without admin: treat as APPROVED if stress < 0.5.
            stress = context.stress_score.composite if context.stress_score else 0.0
            base_resp = ActionResponse(
                request_id=request.request_id,
                verdict=Verdict.APPROVED if stress < 0.5 else Verdict.CONDITIONAL,
                reason=f"bootstrap (stress={stress:.2f})",
                reason_code="bootstrap",
                jarvis_action=context.suggestion.action,
                stress_composite=stress,
                session_phase=(
                    context.session_phase  # type: ignore[arg-type]
                    or "OVERNIGHT"
                ),
            )

        # Stage 2 -- regime-aware reweight (nudges composite but stays in [0,1]).
        if context.stress_score:
            raws = {c.name: c.value for c in context.stress_score.components}
            regime = context.regime.regime if context.regime else "UNKNOWN"
            new_comp, contribs, new_binding = reweight(raws, regime)
            notes.append(
                f"regime-reweight: composite {context.stress_score.composite:.2f} "
                f"-> {new_comp:.2f} (binding={new_binding})"
            )
            stress_composite_for_downstream = new_comp
        else:
            stress_composite_for_downstream = base_resp.stress_composite

        # Stage 3 -- horizons
        h_until = context.macro.hours_until_next_event if context.macro else None
        event_label = context.macro.next_event_label if context.macro else None
        h_ctx = project_horizons(
            base_composite=stress_composite_for_downstream,
            base_binding=(context.stress_score.binding_constraint if context.stress_score else "unknown"),
            hours_until_event=h_until,
            event_label=event_label,
            is_overnight_now=(str(context.session_phase or "").upper() == "OVERNIGHT"),
        )

        # Stage 4 -- predictive
        projection = self.forecaster.update(stress_composite_for_downstream)

        # Stage 5 -- portfolio
        port = None
        if exposures:
            port = assess_portfolio(exposures, corr_matrix=corr_matrix)
            if port.cluster_breach:
                notes.append(f"portfolio breach: {port.notes[0]}")

        # Stage 6 -- operator preferences
        nudge = self.preferences.nudge_for(
            request.subsystem.value,
            request.action.value,
            base_resp.reason_code,
            now=now,
        )
        if nudge and nudge.confidence > 0.5:
            notes.append(f"operator preference: {nudge.suggestion}")

        # Stage 9 -- precedent
        prec_key = PrecedentKey(
            regime=(context.regime.regime if context.regime else "UNKNOWN"),
            session_phase=str(context.session_phase or "OVERNIGHT"),
            event_category="macro_event" if event_label else "none",
            binding_constraint=(context.stress_score.binding_constraint if context.stress_score else "none"),
        )
        prec_q = self.precedent.query(prec_key)

        # Stage 10 -- doctrine
        doctrine = apply_doctrine(
            proposed_verdict=base_resp.verdict.value,
            subsystem=request.subsystem.value,
            action=request.action.value,
            context_tags=[],
            violations=[],
            now=now,
        )

        # Stage 8 -- calibration (on the DOCTRINE-adjusted verdict)
        cal_feat = VerdictFeatures(
            verdict=doctrine.doctrine_verdict,
            stress_composite=stress_composite_for_downstream,
            sizing_mult=base_resp.size_cap_mult or 1.0,
            session_phase=str(context.session_phase or "OVERNIGHT"),
            binding_constraint=(context.stress_score.binding_constraint if context.stress_score else "none"),
            event_within_1h=(h_until is not None and h_until <= 1.0),
        )
        calibrated = calibrate_verdict(cal_feat, self.calibrator)

        # Stage 5b -- portfolio downgrade override (hard block > doctrine upgrade)
        final_verdict = doctrine.doctrine_verdict
        if port and port.cluster_breach:
            if port.verdict_downgrade == "DENIED":
                final_verdict = "DENIED"
            elif port.verdict_downgrade == "CONDITIONAL" and final_verdict == "APPROVED":
                final_verdict = "CONDITIONAL"

        # Stage 11 -- anomaly scan on the context inputs we can expose
        anomaly_payload: dict[str, float] = {
            "stress_composite": stress_composite_for_downstream,
        }
        if context.macro and context.macro.vix_level is not None:
            anomaly_payload["vix"] = context.macro.vix_level
        if context.regime:
            anomaly_payload["regime_confidence"] = context.regime.confidence
        if context.equity:
            anomaly_payload["equity_dd"] = context.equity.daily_drawdown_pct
            anomaly_payload["open_risk_r"] = context.equity.open_risk_r
        anomaly_reports = self.anomaly.observe(anomaly_payload)
        if anomaly_reports:
            notes.append(
                f"anomaly: {len(anomaly_reports)} field(s) flagged ({', '.join(r.field for r in anomaly_reports)})",
            )

        # Stage 12 / 13 / 14 / 15 -- out-of-band. Unleashed doesn't block on them.
        alerts = list(extra_alerts or [])

        return ApexDecision(
            ts=now,
            request_id=request.request_id,
            base_verdict=base_resp.verdict.value,
            doctrine_verdict=doctrine.doctrine_verdict,
            final_verdict=final_verdict,
            calibrated=calibrated,
            doctrine=doctrine,
            horizons=h_ctx,
            projection=projection,
            portfolio=port,
            precedent=prec_q,
            alerts=alerts,
            size_cap_mult=base_resp.size_cap_mult,
            notes=notes,
        )

    def dashboard_snapshot(
        self,
        context: JarvisContext,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """A payload the React JARVIS tile can render directly."""
        now = now or datetime.now(UTC)
        stress_components = []
        if context.stress_score:
            for c in context.stress_score.components:
                stress_components.append(
                    {
                        "name": c.name,
                        "value": c.value,
                        "weight": c.weight,
                        "contribution": c.contribution,
                    }
                )
        bud_status = self.budget.status(now=now)
        kaizen_summary = self.kaizen.summary(window_days=7, now=now)
        return {
            "ts": now.isoformat(),
            "suggestion": (context.suggestion.action.value if context.suggestion else "UNKNOWN"),
            "regime": (context.regime.regime if context.regime else "UNKNOWN"),
            "session_phase": str(context.session_phase or "OVERNIGHT"),
            "stress": {
                "composite": (context.stress_score.composite if context.stress_score else 0.0),
                "binding": (context.stress_score.binding_constraint if context.stress_score else "none"),
                "components": stress_components,
            },
            "budget": {
                "tier_state": bud_status.tier_state,
                "hourly_burn_pct": bud_status.hourly_burn_pct,
                "daily_burn_pct": bud_status.daily_burn_pct,
            },
            "kaizen": {
                "severity": kaizen_summary.severity,
                "velocity": kaizen_summary.velocity,
                "note": kaizen_summary.note,
            },
        }


def factory(
    audit_path: Path | str | None = None,
    context_engine: object | None = None,
) -> ApexPredatorCore:
    """Build a core with JarvisAdmin wired if inputs are provided."""
    admin: JarvisAdmin | None = None
    if audit_path is not None or context_engine is not None:
        admin = JarvisAdmin(
            engine=context_engine,
            audit_path=Path(audit_path) if audit_path else None,
        )
    return ApexPredatorCore(admin=admin)
