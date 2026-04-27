"""
JARVIS v3 // claude_layer tests
===============================
Covers escalation / stakes / prompt_cache / distillation / usage_tracker
/ prompts / cost_governor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.brain.jarvis_v3.claude_layer import (
    cost_governor,
    distillation,
    escalation,
    prompt_cache,
    prompts,
    stakes,
    usage_tracker,
)
from eta_engine.brain.model_policy import ModelTier

# ---------------------------------------------------------------------------
# Layer 1 -- escalation
# ---------------------------------------------------------------------------


class TestEscalation:
    def test_quiet_context_no_escalate(self):
        inp = escalation.EscalationInputs(
            regime="NEUTRAL",
            stress_composite=0.2,
            sizing_mult=0.9,
            action="ORDER_PLACE",
            precedent_n=50,
        )
        d = escalation.should_escalate(inp)
        assert not d.escalate
        assert d.jarvis_handles

    def test_crisis_escalates(self):
        inp = escalation.EscalationInputs(
            regime="CRISIS",
            stress_composite=0.3,
            sizing_mult=0.9,
            precedent_n=10,
        )
        d = escalation.should_escalate(inp)
        assert d.escalate
        assert escalation.EscalationTrigger.CRISIS_REGIME in d.triggers

    def test_critical_action_always_escalates(self):
        inp = escalation.EscalationInputs(
            action="STRATEGY_DEPLOY",
            stress_composite=0.1,
            precedent_n=100,
        )
        d = escalation.should_escalate(inp)
        assert d.escalate

    def test_empty_precedent_escalates(self):
        inp = escalation.EscalationInputs(
            regime="NEUTRAL",
            stress_composite=0.2,
            precedent_n=0,
        )
        d = escalation.should_escalate(inp)
        assert escalation.EscalationTrigger.PRECEDENT_EMPTY in d.triggers

    def test_escalation_rate_computation(self):
        decs = [
            escalation.should_escalate(
                escalation.EscalationInputs(
                    regime="NEUTRAL",
                    stress_composite=0.2,
                    precedent_n=10,
                )
            ),
            escalation.should_escalate(
                escalation.EscalationInputs(
                    regime="CRISIS",
                    precedent_n=10,
                )
            ),
        ]
        esc, total, rate = escalation.escalation_rate(decs)
        assert total == 2
        assert esc == 1
        assert rate == 0.5


# ---------------------------------------------------------------------------
# Layer 3 -- stakes
# ---------------------------------------------------------------------------


class TestStakes:
    def test_default_medium(self):
        v = stakes.classify_stakes(stakes.StakesInputs())
        assert v.stakes == stakes.Stakes.MEDIUM or v.stakes == stakes.Stakes.LOW

    def test_critical_action_pins(self):
        v = stakes.classify_stakes(
            stakes.StakesInputs(
                action="KILL_SWITCH_RESET",
            )
        )
        assert v.stakes == stakes.Stakes.CRITICAL
        assert v.model_tier == ModelTier.OPUS

    def test_live_bumps_to_high(self):
        v = stakes.classify_stakes(
            stakes.StakesInputs(
                is_live=True,
                r_at_risk=1.0,
                action="ORDER_PLACE",
            )
        )
        assert v.stakes.value in {"HIGH", "CRITICAL"}

    def test_high_r_at_risk_critical(self):
        v = stakes.classify_stakes(
            stakes.StakesInputs(
                r_at_risk=3.5,
                action="ORDER_PLACE",
            )
        )
        assert v.stakes == stakes.Stakes.CRITICAL

    def test_skeptic_opus_at_high(self):
        v = stakes.classify_stakes(
            stakes.StakesInputs(
                is_live=True,
                r_at_risk=1.6,
            )
        )
        assert v.skeptic_tier == ModelTier.OPUS


# ---------------------------------------------------------------------------
# Layer 2 -- prompt_cache
# ---------------------------------------------------------------------------


class TestPromptCache:
    def test_prompt_splits(self):
        p = prompt_cache.build_cached_prompt(
            system="test",
            prefix="prefix content" * 100,
            suffix="suffix",
        )
        assert p.tokens_prefix > p.tokens_suffix
        assert len(p.prefix_hash) >= 8

    def test_cache_tracker_first_call_miss(self):
        tr = prompt_cache.PromptCacheTracker()
        hit = tr.observe("abc")
        assert not hit

    def test_cache_tracker_refresh_on_second_call(self):
        tr = prompt_cache.PromptCacheTracker()
        tr.observe("abc")
        hit = tr.observe("abc")
        assert hit

    def test_cache_tracker_expires(self):
        tr = prompt_cache.PromptCacheTracker(ttl_s=60)
        ts = datetime.now(UTC)
        tr.observe("abc", now=ts)
        hit = tr.observe("abc", now=ts + timedelta(minutes=10))
        assert not hit

    def test_cost_estimation_cache_hit_cheaper(self):
        miss = prompt_cache.estimate_cost(
            ModelTier.SONNET,
            prefix_tokens=2000,
            suffix_tokens=500,
            output_tokens=300,
            cache_hit=False,
        )
        hit = prompt_cache.estimate_cost(
            ModelTier.SONNET,
            prefix_tokens=2000,
            suffix_tokens=500,
            output_tokens=300,
            cache_hit=True,
        )
        assert hit < miss
        assert hit < miss * 0.8  # at least 20% cheaper

    def test_fake_client_deterministic(self):
        client = prompt_cache.FakeClaudeClient()
        p = prompt_cache.build_cached_prompt(
            system="s",
            prefix="prefix" * 200,
            suffix="suffix",
        )
        req = prompt_cache.ClaudeCallRequest(
            model=ModelTier.HAIKU,
            prompt=p,
            persona="BULL",
        )
        r = client.call(req)
        assert r.cost_usd > 0
        assert r.persona == "BULL"
        # Second call hits cache
        r2 = client.call(req)
        assert r2.cache_hit


# ---------------------------------------------------------------------------
# Layer 4 -- distillation
# ---------------------------------------------------------------------------


class TestDistillation:
    def test_empty_classifier_uncertain(self):
        d = distillation.Distiller()
        skip = d.should_skip({"stress_composite": 0.3})
        assert 0.0 <= skip.p_agree <= 1.0
        # With no training, we should NOT skip Claude (safe default)
        assert not skip.skip_claude

    def test_fit_learns_pattern(self):
        # When agreement is the label and stress is high, label=1 (agree -- both deny)
        # When stress is low, label=0 (disagree -- Claude approves, JARVIS denies)
        samples = []
        for _ in range(20):
            samples.append(
                distillation.DistillSample(
                    features={"stress_composite": 0.9},
                    deterministic_verdict="DENY",
                    claude_verdict="DENY",
                )
            )
        for _ in range(20):
            samples.append(
                distillation.DistillSample(
                    features={"stress_composite": 0.1},
                    deterministic_verdict="DENY",
                    claude_verdict="APPROVE",
                )
            )
        d = distillation.Distiller()
        d.fit(samples, iters=200)
        # High stress -> both agree (label=1) -> high p_agree
        high = d.should_skip({"stress_composite": 0.9})
        low = d.should_skip({"stress_composite": 0.1})
        assert high.p_agree > low.p_agree

    def test_save_load_roundtrip(self, tmp_path):
        d = distillation.Distiller()
        samples = [
            distillation.DistillSample(
                features={"stress_composite": 0.5},
                deterministic_verdict="CONDITIONAL",
                claude_verdict="CONDITIONAL",
            )
            for _ in range(10)
        ]
        d.fit(samples, iters=50)
        path = tmp_path / "distiller.json"
        d.save(path)
        d2 = distillation.Distiller.load(path)
        assert d2.model.train_n == 10
        assert d2.model.version >= 1


# ---------------------------------------------------------------------------
# usage_tracker
# ---------------------------------------------------------------------------


class TestUsageTracker:
    def test_empty_is_ok(self):
        u = usage_tracker.UsageTracker()
        q = u.quota_state()
        assert q.state == usage_tracker.QuotaState.OK

    def test_exceed_budget_freezes(self):
        u = usage_tracker.UsageTracker(
            hourly_usd_budget=0.01,
            daily_usd_budget=0.01,
        )
        # Log one call that blows the budget
        res = prompt_cache.ClaudeCallResult(
            model=ModelTier.OPUS,
            persona="SKEPTIC",
            output_text="x",
            input_tokens=1000,
            output_tokens=300,
            cached_read_tokens=0,
            cache_write_tokens=1000,
            cost_usd=0.10,
            cache_hit=False,
            ts=datetime.now(UTC),
        )
        u.record_call(res)
        q = u.quota_state()
        assert q.state == usage_tracker.QuotaState.FREEZE

    def test_cache_hit_rate_tracked(self):
        u = usage_tracker.UsageTracker()
        now = datetime.now(UTC)
        for i in range(10):
            u.record_call(
                prompt_cache.ClaudeCallResult(
                    model=ModelTier.SONNET,
                    persona="BULL",
                    output_text="x",
                    input_tokens=100,
                    output_tokens=50,
                    cached_read_tokens=90 if i > 0 else 0,
                    cache_write_tokens=0 if i > 0 else 90,
                    cost_usd=0.001,
                    cache_hit=(i > 0),
                    ts=now,
                )
            )
        rate = u.cache_hit_rate(now=now)
        assert rate == 0.9


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


class TestPrompts:
    def test_all_personas_have_prefixes(self):
        assert set(prompts.PERSONA_PREFIXES) == {"BULL", "BEAR", "SKEPTIC", "HISTORIAN"}

    def test_prefix_includes_doctrine(self):
        for _name, pfx in prompts.PERSONA_PREFIXES.items():
            assert "EVOLUTIONARY TRADING ALGO DOCTRINE" in pfx
            assert "OUTPUT FORMAT" in pfx

    def test_suffix_render_compact(self):
        ctx = prompts.StructuredContext(
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
            precedent_n=10,
            precedent_win_rate=0.6,
            precedent_mean_r=0.5,
            operator_overrides_24h=1,
            jarvis_baseline_verdict="APPROVED",
        )
        s = prompts.render_suffix(ctx)
        assert "stress_composite" in s
        assert "JARVIS_BASELINE_VERDICT" in s

    def test_parse_verdict_basic(self):
        text = "VOTE: APPROVE\nCONFIDENCE: 0.82\n- reason 1\n- reason 2\n* evidence"
        v = prompts.parse_verdict(text)
        assert v.vote == "APPROVE"
        assert v.confidence == 0.82
        assert len(v.reasons) == 2
        assert len(v.evidence) == 1

    def test_parse_verdict_forgiving(self):
        v = prompts.parse_verdict("garbled nonsense")
        assert v.vote == "CONDITIONAL"
        assert v.confidence == 0.0


# ---------------------------------------------------------------------------
# cost_governor
# ---------------------------------------------------------------------------


class TestCostGovernor:
    def test_no_escalation_returns_false(self):
        u = usage_tracker.UsageTracker()
        cg = cost_governor.CostGovernor(u)
        plan = cg.plan(
            escalation_inputs=escalation.EscalationInputs(
                regime="NEUTRAL",
                stress_composite=0.2,
                precedent_n=20,
            ),
            stakes_inputs=stakes.StakesInputs(),
            features={"stress_composite": 0.2},
        )
        assert not plan.invoke_claude

    def test_escalation_on_crisis(self):
        u = usage_tracker.UsageTracker()
        cg = cost_governor.CostGovernor(u)
        plan = cg.plan(
            escalation_inputs=escalation.EscalationInputs(
                regime="CRISIS",
                precedent_n=10,
            ),
            stakes_inputs=stakes.StakesInputs(regime="CRISIS"),
            features={"stress_composite": 0.5, "regime": "CRISIS"},
        )
        assert plan.invoke_claude
        assert plan.stakes is not None
        assert len(plan.personas) == 4

    def test_freeze_disables_claude(self):
        u = usage_tracker.UsageTracker(
            hourly_usd_budget=0.01,
            daily_usd_budget=0.01,
        )
        u.record_call(
            prompt_cache.ClaudeCallResult(
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
        cg = cost_governor.CostGovernor(u)
        plan = cg.plan(
            escalation_inputs=escalation.EscalationInputs(regime="CRISIS"),
            stakes_inputs=stakes.StakesInputs(regime="CRISIS"),
            features={"stress_composite": 0.5},
        )
        assert not plan.invoke_claude
        # All personas should be deterministic under FREEZE
        assert all(p.deterministic for p in plan.personas)

    def test_downshift_demotes_tiers(self):
        # Artificially push to DOWNSHIFT state
        u = usage_tracker.UsageTracker(
            hourly_usd_budget=1.0,
            daily_usd_budget=1.0,
        )
        u.record_call(
            prompt_cache.ClaudeCallResult(
                model=ModelTier.SONNET,
                persona="x",
                output_text="x",
                input_tokens=100,
                output_tokens=50,
                cached_read_tokens=0,
                cache_write_tokens=100,
                cost_usd=0.85,  # 85% of budget -> DOWNSHIFT (>= 80% threshold)
                cache_hit=False,
                ts=datetime.now(UTC),
            )
        )
        cg = cost_governor.CostGovernor(u)
        plan = cg.plan(
            escalation_inputs=escalation.EscalationInputs(
                regime="NEUTRAL",
                stress_composite=0.7,
                precedent_n=10,
            ),
            stakes_inputs=stakes.StakesInputs(is_live=True, r_at_risk=1.6),
            features={"stress_composite": 0.7},
        )
        if plan.invoke_claude:
            # Under DOWNSHIFT, Opus should be demoted to Sonnet
            for p in plan.personas:
                if p.tier is not None:
                    assert p.tier != ModelTier.OPUS
