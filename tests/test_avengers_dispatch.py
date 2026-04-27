"""
Tests for brain.avengers.dispatch -- the bridge between claude_layer
and the Avengers crew that takes weight off JARVIS.
"""

from __future__ import annotations

from datetime import UTC, datetime

from eta_engine.brain.avengers import (
    TASK_CADENCE,
    TASK_OWNERS,
    AvengersDispatch,
    BackgroundTask,
    DispatchRoute,
    DryRunExecutor,
    Fleet,
)
from eta_engine.brain.jarvis_v3.claude_layer.cost_governor import (
    CostGovernor,
)
from eta_engine.brain.jarvis_v3.claude_layer.escalation import (
    EscalationInputs,
)
from eta_engine.brain.jarvis_v3.claude_layer.prompts import (
    StructuredContext,
)
from eta_engine.brain.jarvis_v3.claude_layer.stakes import StakesInputs
from eta_engine.brain.jarvis_v3.claude_layer.usage_tracker import (
    UsageTracker,
)


def _ctx(**overrides) -> StructuredContext:
    base = dict(
        ts="2026-04-23T14:00",
        subsystem="bot.mnq",
        action="ORDER_PLACE",
        regime="NEUTRAL",
        regime_confidence=0.8,
        session_phase="MORNING",
        stress_composite=0.3,
        binding_constraint="equity_dd",
        sizing_mult=0.9,
        hours_until_event=None,
        event_label=None,
        r_at_risk=1.0,
        daily_dd_pct=0.01,
        portfolio_breach=False,
        doctrine_net_bias=-0.1,
        doctrine_tenets=["CAPITAL_FIRST"],
        precedent_n=10,
        precedent_win_rate=0.6,
        precedent_mean_r=0.5,
        anomaly_flags=[],
        operator_overrides_24h=1,
        jarvis_baseline_verdict="APPROVED",
    )
    base.update(overrides)
    return StructuredContext(**base)


def _fleet() -> Fleet:
    """Fleet wired to a DryRun executor -- no network."""
    return Fleet(executor=DryRunExecutor())


class TestAvengersDispatch:
    def test_no_escalation_returns_jarvis_only(self):
        fleet = _fleet()
        gov = CostGovernor(UsageTracker())
        dispatch = AvengersDispatch(governor=gov, fleet=fleet)
        result = dispatch.decide(
            escalation_inputs=EscalationInputs(
                regime="NEUTRAL",
                stress_composite=0.2,
                precedent_n=20,
            ),
            stakes_inputs=StakesInputs(),
            context=_ctx(),
        )
        assert result.route == DispatchRoute.JARVIS_ONLY
        assert result.claude_debate is None
        assert result.final_vote in {"APPROVE", "CONDITIONAL", "DENY", "DEFER"}

    def test_crisis_triggers_batman(self):
        fleet = _fleet()
        gov = CostGovernor(UsageTracker())
        dispatch = AvengersDispatch(governor=gov, fleet=fleet)
        result = dispatch.decide(
            escalation_inputs=EscalationInputs(
                regime="CRISIS",
                stress_composite=0.7,
                precedent_n=10,
            ),
            stakes_inputs=StakesInputs(regime="CRISIS", r_at_risk=1.5),
            context=_ctx(regime="CRISIS", stress_composite=0.7, r_at_risk=1.5),
        )
        assert result.route == DispatchRoute.BATMAN_DEBATE
        assert result.claude_debate is not None
        # At least the 3 Claude-backed personas fired (Bull/Bear may be deterministic
        # at non-critical; skeptic/historian always claude at HIGH+)
        assert len(result.claude_debate) >= 1

    def test_quota_freeze_returns_jarvis_freeze(self):
        from eta_engine.brain.jarvis_v3.claude_layer.prompt_cache import (
            ClaudeCallResult,
        )
        from eta_engine.brain.model_policy import ModelTier

        u = UsageTracker(hourly_usd_budget=0.01, daily_usd_budget=0.01)
        u.record_call(
            ClaudeCallResult(
                model=ModelTier.OPUS,
                persona="X",
                output_text="x",
                input_tokens=1000,
                output_tokens=300,
                cached_read_tokens=0,
                cache_write_tokens=1000,
                cost_usd=0.10,
                cache_hit=False,
                ts=datetime.now(UTC),
            )
        )
        fleet = _fleet()
        gov = CostGovernor(u)
        dispatch = AvengersDispatch(governor=gov, fleet=fleet)
        result = dispatch.decide(
            escalation_inputs=EscalationInputs(regime="CRISIS", precedent_n=10),
            stakes_inputs=StakesInputs(regime="CRISIS"),
            context=_ctx(regime="CRISIS", stress_composite=0.6),
        )
        assert result.route == DispatchRoute.JARVIS_FREEZE
        assert result.claude_debate is None

    def test_deterministic_debate_always_available(self):
        """Even when Claude fires, we always have the deterministic fallback."""
        fleet = _fleet()
        gov = CostGovernor(UsageTracker())
        dispatch = AvengersDispatch(governor=gov, fleet=fleet)
        result = dispatch.decide(
            escalation_inputs=EscalationInputs(regime="NEUTRAL", precedent_n=50),
            stakes_inputs=StakesInputs(),
            context=_ctx(),
        )
        assert result.deterministic is not None
        assert len(result.deterministic.transcript) == 4


class TestBackgroundTaskRouting:
    def test_all_tasks_have_owner(self):
        for task in BackgroundTask:
            assert task in TASK_OWNERS
            owner = TASK_OWNERS[task]
            assert owner in {"ALFRED", "BATMAN", "ROBIN"}

    def test_all_tasks_have_cadence(self):
        for task in BackgroundTask:
            assert task in TASK_CADENCE
            # Very loose cron-format sniff: non-empty string with spaces
            assert " " in TASK_CADENCE[task]

    def test_alfred_owns_operational_maintenance(self):
        assert TASK_OWNERS[BackgroundTask.KAIZEN_RETRO] == "ALFRED"
        assert TASK_OWNERS[BackgroundTask.DISTILL_TRAIN] == "ALFRED"
        assert TASK_OWNERS[BackgroundTask.SHADOW_TICK] == "ALFRED"
        assert TASK_OWNERS[BackgroundTask.DRIFT_SUMMARY] == "ALFRED"

    def test_batman_owns_strategic(self):
        assert TASK_OWNERS[BackgroundTask.STRATEGY_MINE] == "BATMAN"
        assert TASK_OWNERS[BackgroundTask.CAUSAL_REVIEW] == "BATMAN"
        assert TASK_OWNERS[BackgroundTask.TWIN_VERDICT] == "BATMAN"
        assert TASK_OWNERS[BackgroundTask.DOCTRINE_REVIEW] == "BATMAN"

    def test_robin_owns_grunt(self):
        assert TASK_OWNERS[BackgroundTask.LOG_COMPACT] == "ROBIN"
        assert TASK_OWNERS[BackgroundTask.PROMPT_WARMUP] == "ROBIN"
        assert TASK_OWNERS[BackgroundTask.DASHBOARD_ASSEMBLE] == "ROBIN"
        assert TASK_OWNERS[BackgroundTask.AUDIT_SUMMARIZE] == "ROBIN"

    def test_hot_path_tasks_are_short_cadence(self):
        # Tasks that need to run frequently during market hours
        hot = (BackgroundTask.SHADOW_TICK, BackgroundTask.DRIFT_SUMMARY, BackgroundTask.DASHBOARD_ASSEMBLE)
        for t in hot:
            assert TASK_CADENCE[t].startswith("*")
