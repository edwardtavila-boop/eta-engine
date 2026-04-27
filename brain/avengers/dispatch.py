"""
EVOLUTIONARY TRADING ALGO // brain.avengers.dispatch
========================================
The load-balancer between JARVIS hot path and the Avengers crew.

Operator directive (2026-04-23): "JARVIS needs to be lighter to execute.
Put weight on BATMAN, ALFRED, and ROBIN so JARVIS stays on the hot path
only."

This module is the bridge between the v3 ``claude_layer`` (which decides
WHEN Claude should be invoked and at what tier) and the existing
``brain.avengers.fleet`` (which owns the actual personas). After this
module lands, JARVIS's hot path contains ZERO LLM work. Every reasoning
task -- debate orchestration, distillation training, Kaizen
retrospectives, shadow-trade resolution, prompt-prefix warmup, audit
compaction -- is delegated to one of:

  * BATMAN -- Opus-tier: Claude-backed persona debate, strategy review,
              digital-twin promote/avoid verdict, causal-ATE review
  * ALFRED -- Sonnet-tier: Kaizen retrospective drafting, distillation
              training prep, shadow-trade analysis, drift explanations
  * ROBIN  -- Haiku-tier: prompt cache warmup, audit log compaction,
              dashboard payload assembly, log tail summarization

JARVIS's role is reduced to:
  1. Run cost_governor.plan() (cheap deterministic check)
  2. If invoke_claude=True, hand off to BATMAN
  3. If invoke_claude=False, return JARVIS's own deterministic verdict
  4. Never touch Anthropic SDK itself

Pure stdlib + pydantic. No network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.jarvis_v3.claude_layer.cost_governor import (
    CostGovernor,  # noqa: TC001  (pydantic field at runtime)
    InvocationPlan,  # noqa: TC001  (pydantic field at runtime)
)
from eta_engine.brain.jarvis_v3.claude_layer.escalation import (
    EscalationInputs,  # noqa: TC001  (runtime method param)
)
from eta_engine.brain.jarvis_v3.claude_layer.prompts import (
    ParsedVerdict,
    StructuredContext,
    build_persona_prompts,
    parse_verdict,
)
from eta_engine.brain.jarvis_v3.claude_layer.stakes import StakesInputs  # noqa: TC001
from eta_engine.brain.jarvis_v3.next_level.debate import (
    DebateVerdict,
)
from eta_engine.brain.jarvis_v3.next_level.debate import (
    full_debate as deterministic_debate,
)
from eta_engine.brain.model_policy import TaskCategory

if TYPE_CHECKING:
    from eta_engine.brain.avengers.fleet import Fleet


class DispatchRoute(StrEnum):
    """Which path the dispatcher took for this decision."""

    JARVIS_ONLY = "JARVIS_ONLY"  # gate said no-escalate
    JARVIS_DISTILL = "JARVIS_DISTILL"  # distiller said classifier is confident
    JARVIS_FREEZE = "JARVIS_FREEZE"  # quota freeze
    BATMAN_DEBATE = "BATMAN_DEBATE"  # Claude-backed debate via BATMAN
    BATMAN_REVIEW = "BATMAN_REVIEW"  # heavy strategic review (single-shot)


class DispatchResult(BaseModel):
    """What the dispatcher returns to JARVIS's hot path."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    route: DispatchRoute
    plan: InvocationPlan
    deterministic: DebateVerdict  # always available as fallback
    claude_debate: dict[str, ParsedVerdict] | None = None
    final_vote: str = Field(min_length=1)
    note: str = ""


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------


class AvengersDispatch:
    """Single entry point JARVIS uses to delegate reasoning.

    Constructor injection:
      * ``governor``  -- cost_governor.CostGovernor (already wired to
                         usage_tracker + distiller)
      * ``fleet``     -- avengers.fleet.Fleet (already wired to personas)

    The dispatcher itself holds NO state. All persistent state lives in
    the governor / fleet / usage_tracker / distiller.
    """

    def __init__(self, governor: CostGovernor, fleet: Fleet) -> None:
        self.governor = governor
        self.fleet = fleet

    # ---------------------------------------------------------------
    # Hot-path entry
    # ---------------------------------------------------------------
    def decide(
        self,
        *,
        escalation_inputs: EscalationInputs,
        stakes_inputs: StakesInputs,
        context: StructuredContext,
        deterministic_debate_kwargs: dict | None = None,
        now: datetime | None = None,
    ) -> DispatchResult:
        """Hot-path decision.

        1. Always run the deterministic debate first (cheap + local).
        2. Ask the governor if Claude should be invoked.
        3. If yes, delegate to BATMAN. Otherwise return the deterministic
           debate and note WHY Claude was skipped.
        """
        now = now or datetime.now(UTC)
        # 1. Cheap deterministic debate is ALWAYS done (JARVIS is free).
        det = deterministic_debate(
            **(deterministic_debate_kwargs or self._debate_kwargs_from_ctx(context)),
            now=now,
        )

        # 2. Governor decides whether to invoke Claude.
        plan = self.governor.plan(
            escalation_inputs=escalation_inputs,
            stakes_inputs=stakes_inputs,
            features=_features_from(context),
        )

        # 3. Branch on governor verdict.
        if not plan.invoke_claude:
            route = self._route_from_plan(plan)
            return DispatchResult(
                ts=now,
                route=route,
                plan=plan,
                deterministic=det,
                claude_debate=None,
                final_vote=det.final_vote,
                note=plan.reason,
            )

        # 4. Claude-backed: BATMAN orchestrates the debate.
        claude_results = self._run_claude_debate(
            plan=plan,
            context=context,
            baseline=det,
        )
        # 5. Reconcile: Claude votes trump deterministic if they agree
        #    internally; otherwise fall back to deterministic verdict.
        final = self._reconcile(claude_results, det)
        return DispatchResult(
            ts=now,
            route=DispatchRoute.BATMAN_DEBATE,
            plan=plan,
            deterministic=det,
            claude_debate=claude_results,
            final_vote=final,
            note=f"BATMAN-orchestrated debate across {len(claude_results)} personas",
        )

    # ---------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------
    def _route_from_plan(self, plan: InvocationPlan) -> DispatchRoute:
        """Map a no-invoke plan to a dispatch route."""
        if plan.escalation.jarvis_handles:
            return DispatchRoute.JARVIS_ONLY
        if plan.distillation is not None and plan.distillation.skip_claude:
            return DispatchRoute.JARVIS_DISTILL
        if plan.quota is not None and plan.quota.state.value == "FREEZE":
            return DispatchRoute.JARVIS_FREEZE
        return DispatchRoute.JARVIS_ONLY

    def _debate_kwargs_from_ctx(
        self,
        ctx: StructuredContext,
    ) -> dict:
        """Translate StructuredContext -> deterministic debate kwargs."""
        return {
            "stress": ctx.stress_composite,
            "sizing_mult": ctx.sizing_mult,
            "regime": ctx.regime,
            "regime_confidence": ctx.regime_confidence,
            "suggestion": ctx.jarvis_baseline_verdict or "TRADE",
            "dd_pct": ctx.daily_dd_pct,
            "events_count": (1 if ctx.event_label else 0),
            "precedent_n": ctx.precedent_n,
            "precedent_win_rate": ctx.precedent_win_rate,
            "precedent_mean_r": ctx.precedent_mean_r,
            "precedent_suggestion": "",
        }

    def _run_claude_debate(
        self,
        *,
        plan: InvocationPlan,
        context: StructuredContext,
        baseline: DebateVerdict,
    ) -> dict[str, ParsedVerdict]:
        """Spawn BATMAN-orchestrated persona calls via the Fleet.

        For each non-deterministic persona in the plan, we send a
        ``TaskEnvelope`` to BATMAN (who owns the debate orchestration).
        BATMAN in turn uses the prompt_cache wrapper to call Claude at
        the assigned tier.

        Because actual Claude calls are out of scope for this pure
        module (network I/O), we rely on the Fleet's injected executor.
        Tests swap in a fake executor; production wires in the real one.
        """
        # Import here to avoid circular (fleet -> dispatch via orchestrator)
        from eta_engine.brain.avengers.base import (
            make_envelope,
        )

        results: dict[str, ParsedVerdict] = {}
        prompts_by_persona = build_persona_prompts(
            [p.persona for p in plan.personas if not p.deterministic],
            context,
        )
        for assignment in plan.personas:
            if assignment.deterministic:
                # Run the deterministic persona -- pull vote from baseline
                results[assignment.persona] = ParsedVerdict(
                    vote=_persona_vote_from_baseline(baseline, assignment.persona),
                    confidence=0.60,
                    reasons=[f"{assignment.persona}: deterministic fallback"],
                    evidence=["no Claude call (JARVIS sufficient)"],
                    raw="(deterministic persona, no Claude)",
                )
                continue
            p_prompts = prompts_by_persona.get(assignment.persona.upper())
            if not p_prompts:
                continue
            envelope = make_envelope(
                category=_category_for_persona(assignment.persona),
                subsystem="persona.jarvis",
                goal=(f"{assignment.persona} persona debate vote for {context.subsystem}/{context.action}"),
                context={
                    "system": p_prompts["system"],
                    "prefix": p_prompts["prefix"],
                    "suffix": p_prompts["suffix"],
                    "persona": assignment.persona,
                    "tier": assignment.tier.value if assignment.tier else "",
                },
                rationale=(
                    f"BATMAN-orchestrated debate, stakes={plan.stakes.stakes.value if plan.stakes else 'UNKNOWN'}"
                ),
                requested_tier=assignment.tier,
            )
            # Fleet routes to the right persona. For persona debate, we
            # always funnel through BATMAN who then calls at the assigned
            # tier via the prompt_cache client.
            task_result = self.fleet.dispatch(envelope)
            vp = parse_verdict(task_result.artifact or "")
            results[assignment.persona] = vp
        return results

    def _reconcile(
        self,
        claude_results: dict[str, ParsedVerdict],
        baseline: DebateVerdict,
    ) -> str:
        """Tally Claude's persona votes; fall back to baseline if deadlock."""
        tally: dict[str, float] = {"APPROVE": 0, "CONDITIONAL": 0, "DENY": 0, "DEFER": 0}
        for v in claude_results.values():
            if v.vote in tally:
                tally[v.vote] += v.confidence or 0.5
        if not tally or sum(tally.values()) == 0:
            return baseline.final_vote
        winner = max(tally.items(), key=lambda kv: kv[1])
        # If margin is tight (< 0.15), defer to baseline -- conservative
        sorted_scores = sorted(tally.values(), reverse=True)
        margin = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) > 1 else sorted_scores[0]
        if margin < 0.15:
            return baseline.final_vote
        return winner[0]


def _features_from(ctx: StructuredContext) -> dict[str, float]:
    """Pull the distillation features from a StructuredContext."""
    return {
        "stress_composite": ctx.stress_composite,
        "sizing_mult": ctx.sizing_mult,
        "regime": ctx.regime,
        "hours_until_event": ctx.hours_until_event,
        "portfolio_breach": ctx.portfolio_breach,
        "doctrine_net_bias": ctx.doctrine_net_bias,
        "r_at_risk": ctx.r_at_risk,
        "operator_overrides_24h": ctx.operator_overrides_24h,
        "precedent_n": ctx.precedent_n,
        "anomaly_count": len(ctx.anomaly_flags),
    }


def _persona_vote_from_baseline(baseline: DebateVerdict, persona: str) -> str:
    """Retrieve the deterministic persona's vote from the baseline transcript."""
    for arg in baseline.transcript:
        if arg.persona.value == persona.upper():
            return arg.vote
    return baseline.final_vote


def _category_for_persona(persona: str) -> TaskCategory:
    """Map persona role to the TaskCategory that triggers the right tier."""
    p = persona.upper()
    if p == "SKEPTIC":
        # Skeptic is where the adversarial reasoning lives
        return TaskCategory.ADVERSARIAL_REVIEW
    if p in {"BULL", "BEAR"}:
        return TaskCategory.STRATEGY_EDIT
    if p == "HISTORIAN":
        return TaskCategory.TRIVIAL_LOOKUP
    return TaskCategory.CODE_REVIEW


# ---------------------------------------------------------------------------
# Background task dispatchers -- called by ALFRED / ROBIN cron hooks
# ---------------------------------------------------------------------------


class BackgroundTask(StrEnum):
    """Out-of-band tasks that were once on JARVIS but now belong to Avengers."""

    KAIZEN_RETRO = "KAIZEN_RETRO"  # ALFRED
    DISTILL_TRAIN = "DISTILL_TRAIN"  # ALFRED
    SHADOW_TICK = "SHADOW_TICK"  # ALFRED
    DRIFT_SUMMARY = "DRIFT_SUMMARY"  # ALFRED
    STRATEGY_MINE = "STRATEGY_MINE"  # BATMAN
    CAUSAL_REVIEW = "CAUSAL_REVIEW"  # BATMAN
    TWIN_VERDICT = "TWIN_VERDICT"  # BATMAN
    DOCTRINE_REVIEW = "DOCTRINE_REVIEW"  # BATMAN
    LOG_COMPACT = "LOG_COMPACT"  # ROBIN
    PROMPT_WARMUP = "PROMPT_WARMUP"  # ROBIN
    DASHBOARD_ASSEMBLE = "DASHBOARD_ASSEMBLE"  # ROBIN
    AUDIT_SUMMARIZE = "AUDIT_SUMMARIZE"  # ROBIN
    META_UPGRADE = "META_UPGRADE"  # ALFRED -- daily self-update
    CHAOS_DRILL = "CHAOS_DRILL"  # ALFRED -- monthly resilience drills
    HEALTH_WATCHDOG = "HEALTH_WATCHDOG"  # ALFRED -- 5-min service auto-heal
    SELF_TEST = "SELF_TEST"  # ALFRED -- daily end-to-end smoke
    LOG_ROTATE = "LOG_ROTATE"  # ROBIN  -- daily log archive/prune
    DISK_CLEANUP = "DISK_CLEANUP"  # ROBIN  -- weekly temp/cache prune
    BACKUP = "BACKUP"  # ALFRED -- daily state+config backup
    PROMETHEUS_EXPORT = "PROMETHEUS_EXPORT"  # ROBIN  -- every minute metrics flush


# Which persona owns which task. Used by the cron wrapper in scripts/.
TASK_OWNERS: dict[BackgroundTask, str] = {
    BackgroundTask.KAIZEN_RETRO: "ALFRED",
    BackgroundTask.DISTILL_TRAIN: "ALFRED",
    BackgroundTask.SHADOW_TICK: "ALFRED",
    BackgroundTask.DRIFT_SUMMARY: "ALFRED",
    BackgroundTask.STRATEGY_MINE: "BATMAN",
    BackgroundTask.CAUSAL_REVIEW: "BATMAN",
    BackgroundTask.TWIN_VERDICT: "BATMAN",
    BackgroundTask.DOCTRINE_REVIEW: "BATMAN",
    BackgroundTask.LOG_COMPACT: "ROBIN",
    BackgroundTask.PROMPT_WARMUP: "ROBIN",
    BackgroundTask.DASHBOARD_ASSEMBLE: "ROBIN",
    BackgroundTask.AUDIT_SUMMARIZE: "ROBIN",
    BackgroundTask.META_UPGRADE: "ALFRED",
    BackgroundTask.CHAOS_DRILL: "ALFRED",
    BackgroundTask.HEALTH_WATCHDOG: "ALFRED",
    BackgroundTask.SELF_TEST: "ALFRED",
    BackgroundTask.LOG_ROTATE: "ROBIN",
    BackgroundTask.DISK_CLEANUP: "ROBIN",
    BackgroundTask.BACKUP: "ALFRED",
    BackgroundTask.PROMETHEUS_EXPORT: "ROBIN",
}


# Cron cadences. Used by the scheduled-tasks MCP or any equivalent.
TASK_CADENCE: dict[BackgroundTask, str] = {
    BackgroundTask.KAIZEN_RETRO: "0 23 * * *",  # daily 23:00
    BackgroundTask.DISTILL_TRAIN: "0 2 * * 0",  # Sundays 02:00
    BackgroundTask.SHADOW_TICK: "*/5 * * * *",  # every 5 min
    BackgroundTask.DRIFT_SUMMARY: "*/15 * * * *",  # every 15 min
    BackgroundTask.STRATEGY_MINE: "0 3 * * 1",  # Mondays 03:00
    BackgroundTask.CAUSAL_REVIEW: "0 4 1 * *",  # 1st of month 04:00
    BackgroundTask.TWIN_VERDICT: "0 22 * * *",  # daily 22:00
    BackgroundTask.DOCTRINE_REVIEW: "0 5 1 */3 *",  # quarterly
    BackgroundTask.LOG_COMPACT: "0 * * * *",  # hourly
    BackgroundTask.PROMPT_WARMUP: "25,55 13 * * 1-5",  # pre-market + pre-close Mon-Fri
    BackgroundTask.DASHBOARD_ASSEMBLE: "* * * * *",  # every minute
    BackgroundTask.AUDIT_SUMMARIZE: "0 6 * * *",  # daily 06:00
    BackgroundTask.META_UPGRADE: "30 4 * * *",  # daily 04:30 -- git pull + test + restart
    BackgroundTask.CHAOS_DRILL: "0 3 1 * *",  # monthly 1st @ 03:00 -- resilience drills
    BackgroundTask.HEALTH_WATCHDOG: "*/5 * * * *",  # every 5 min -- auto-heal services
    BackgroundTask.SELF_TEST: "0 3 * * *",  # daily 03:00 -- end-to-end smoke
    BackgroundTask.LOG_ROTATE: "0 1 * * *",  # daily 01:00 -- archive + prune logs
    BackgroundTask.DISK_CLEANUP: "0 2 * * 0",  # Sundays 02:00 -- temp/cache prune
    BackgroundTask.BACKUP: "0 5 * * *",  # daily 05:00 -- state+config snapshot
    BackgroundTask.PROMETHEUS_EXPORT: "* * * * *",  # every minute -- OpenMetrics flush
}
