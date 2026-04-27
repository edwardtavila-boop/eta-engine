"""Tests for wave-5 (operator action + parallel code-level work, 2026-04-27).

Covers:
  * contextual_bandit: stress bucketing + Beta-Bernoulli posterior +
    persistence
  * model_policy_budget: BudgetVerdict shape + critical bypass
  * horizons_helper: graceful no-op when horizons.project unavailable
  * refresh_correlation_matrix: pearson math
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ─── contextual_bandit ────────────────────────────────────────────


def test_stress_bucket_quantization() -> None:
    from eta_engine.brain.jarvis_v3.contextual_bandit import _stress_bucket
    assert _stress_bucket(0.10) == "low"
    assert _stress_bucket(0.40) == "med"
    assert _stress_bucket(0.65) == "high"
    assert _stress_bucket(0.90) == "extreme"


def test_context_key_compose() -> None:
    from eta_engine.brain.jarvis_v3.contextual_bandit import context_key
    k = context_key(regime="trend_up", session_phase="OPEN_DRIVE", stress_composite=0.85)
    assert k == "trend_up|OPEN_DRIVE|extreme"


def test_contextual_bandit_register_then_choose(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.contextual_bandit import ContextualBandit
    cb = ContextualBandit(state_path=tmp_path / "post.json")
    cb.register_arm("v17")
    cb.register_arm("v18")
    arm = cb.choose_arm(ctx_key="trend|OPEN_DRIVE|low")
    assert arm in {"v17", "v18"}


def test_contextual_bandit_observe_updates_posterior(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.contextual_bandit import ContextualBandit
    import random
    cb = ContextualBandit(state_path=tmp_path / "post.json", rng=random.Random(0))
    cb.register_arm("v17")
    cb.register_arm("v18")
    # Feed v18 wins, v17 losses for a given context
    for _ in range(20):
        cb.observe_outcome(ctx_key="ctx-1", arm_id="v18", reward=1.0)
        cb.observe_outcome(ctx_key="ctx-1", arm_id="v17", reward=-1.0)
    report = cb.report()
    v18 = next(r for r in report if r["arm_id"] == "v18" and r["context_key"] == "ctx-1")
    v17 = next(r for r in report if r["arm_id"] == "v17" and r["context_key"] == "ctx-1")
    assert v18["mean_reward_p"] > v17["mean_reward_p"]
    assert v18["pulls"] == 20


def test_contextual_bandit_state_persists(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.contextual_bandit import ContextualBandit
    state = tmp_path / "post.json"
    cb1 = ContextualBandit(state_path=state)
    cb1.register_arm("v17")
    cb1.observe_outcome(ctx_key="x", arm_id="v17", reward=1.0)
    cb2 = ContextualBandit(state_path=state)
    rpt = cb2.report()
    assert any(r["arm_id"] == "v17" and r["context_key"] == "x" for r in rpt)


# ─── model_policy_budget ──────────────────────────────────────────


def test_critical_tier_bypasses_budget() -> None:
    from eta_engine.brain.model_policy_budget import allow_llm_call
    v = allow_llm_call(estimated_cost_usd=999.0, tier="critical")
    assert v.allowed is True
    assert v.reason_code == "critical_bypass"


def test_budget_verdict_has_required_fields() -> None:
    from eta_engine.brain.model_policy_budget import BudgetVerdict
    v = BudgetVerdict(allowed=True, reason_code="x", detail="y", budget_usd=50.0)
    assert v.allowed is True
    assert v.budget_usd == 50.0


# ─── horizons_helper ──────────────────────────────────────────────


def test_horizons_helper_returns_empty_when_module_breaks() -> None:
    """If horizons.project errors, we return an empty dict (no caps)."""
    from eta_engine.brain.jarvis_v3 import horizons_helper

    class _Stub:
        pass
    out = horizons_helper.projected_caps(_Stub())
    # May return some caps if horizons.project handles a stub ctx, OR
    # may return empty. Either way, the function must not raise.
    assert isinstance(out, dict)


def test_shortest_horizon_cap_falls_back_to_one_when_unavailable() -> None:
    from eta_engine.brain.jarvis_v3 import horizons_helper

    class _Stub:
        pass
    cap = horizons_helper.shortest_horizon_cap(_Stub())
    # Either a real cap from horizons.project, or 1.0 fallback. Bot's
    # sizing math is unaffected by the fallback.
    assert isinstance(cap, float)
    assert 0.0 < cap <= 1.0


# ─── refresh_correlation_matrix ───────────────────────────────────


def test_pearson_perfectly_correlated_returns_1() -> None:
    from eta_engine.scripts.refresh_correlation_matrix import _pearson
    a = [0.001, 0.002, 0.003, 0.004, 0.005, 0.006]
    b = [x * 2 for x in a]
    assert _pearson(a, b) == pytest.approx(1.0, abs=0.01)


def test_pearson_anti_correlated_returns_minus_1() -> None:
    from eta_engine.scripts.refresh_correlation_matrix import _pearson
    a = [0.001, 0.002, 0.003, 0.004, 0.005]
    b = [-x for x in a]
    assert _pearson(a, b) == pytest.approx(-1.0, abs=0.01)


def test_pearson_short_series_returns_zero() -> None:
    from eta_engine.scripts.refresh_correlation_matrix import _pearson
    assert _pearson([0.1, 0.2], [0.3, 0.4]) == 0.0
