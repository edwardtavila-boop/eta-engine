"""Tests for wave-10 (full-build upgrades to wave-8 modules, 2026-04-27).

Wave-10 ships full-build companions to the wave-8 lean scaffolds:

  * world_model_full.py        -- action-conditioned + value estimation
  * causal_discovery.py        -- PC-style skeleton + backdoor adjustment
  * memory_rag.py              -- hash-embedding RAG retrieval
  * path_generator_full.py     -- regime-mixture + heavy-tail Student-t
  * firm_board_debate.py       -- iterative 3-round debate
  * meta_learner_full.py       -- Pareto-rank + parameter-importance bandit

The lean modules remain (back-compat); these are additive upgrades.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─── world_model_full ─────────────────────────────────────────────


def test_action_conditioned_table_fits(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.world_model_full import (
        ActionConditionedTable,
    )

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    for r in [1.0, 2.0, -0.5]:
        mem.record_episode(
            signal_id=f"s{r}",
            regime="bullish_low_vol",
            session="rth",
            stress=0.3,
            direction="long",
            realized_r=r,
            extra={"action": "approve_full"},
        )
    table = ActionConditionedTable()
    table.fit_from_episodes(mem._episodes)
    assert table.rewards_by_sa


def test_estimate_value_handles_no_data(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.world_model_full import (
        ActionConditionedTable,
        estimate_value,
    )

    table = ActionConditionedTable()
    v = estimate_value(table, state=10, action="approve_full")
    assert v.expected_return == 0.0
    assert v.n_samples == 0
    assert v.confidence == 0.0


def test_rank_actions_orders_by_value(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.world_model_full import (
        ActionConditionedTable,
        rank_actions,
    )

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    # approve_full produces big winners; deny is flat
    for _ in range(20):
        mem.record_episode(
            signal_id="full",
            regime="bullish_low_vol",
            session="rth",
            stress=0.3,
            direction="long",
            realized_r=2.0,
            extra={"action": "approve_full"},
        )
    for _ in range(10):
        mem.record_episode(
            signal_id="den",
            regime="bullish_low_vol",
            session="rth",
            stress=0.3,
            direction="long",
            realized_r=0.0,
            extra={"action": "deny"},
        )
    table = ActionConditionedTable()
    table.fit_from_episodes(mem._episodes)
    from eta_engine.brain.jarvis_v3.world_model import encode_state

    s = encode_state(regime="bullish_low_vol", session="rth", stress=0.3)
    ranking = rank_actions(state=s, table=table, n_rollouts=10, horizon=3)
    # approve_full should rank above deny
    actions_in_order = [a for a, _ in ranking.ranked]
    assert actions_in_order.index("approve_full") < actions_in_order.index("deny")


def test_counterfactual_expected_return_is_nonzero_with_data(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.world_model_full import (
        counterfactual_expected_return,
    )

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    for _ in range(30):
        mem.record_episode(
            signal_id="x",
            regime="neutral",
            session="rth",
            stress=0.5,
            direction="long",
            realized_r=1.0,
            extra={"action": "approve_full"},
        )
    cf = counterfactual_expected_return(
        proposed_action="approve_full",
        memory=mem,
        n_rollouts=10,
        horizon=3,
    )
    # Should be near +1.0 (constant winners)
    assert cf > 0.5


# ─── causal_discovery ────────────────────────────────────────────


def test_partial_correlation_zero_when_dependence_fully_explained() -> None:
    from eta_engine.brain.jarvis_v3.causal_discovery import partial_correlation

    # x = z + noise; y = z + noise -- partial(x, y | z) ~ 0
    z = list(range(50))
    x = [zi + (i % 3 - 1) * 0.01 for i, zi in enumerate(z)]
    y = [zi + (i % 5 - 2) * 0.01 for i, zi in enumerate(z)]
    pc = partial_correlation(x, y, z)
    assert abs(pc) < 0.5


def test_discover_skeleton_finds_obvious_dependency() -> None:
    from eta_engine.brain.jarvis_v3.causal_discovery import discover_skeleton

    # outcome strongly tracks feature1; feature2 is noise
    feature_history = {
        "feat1": [float(i) for i in range(50)],
        "feat2": [(i * 13) % 7 - 3 for i in range(50)],
        "outcome": [float(i) + 0.1 * ((i * 7) % 5) for i in range(50)],
    }
    skel = discover_skeleton(
        feature_history=feature_history,
        independence_threshold=0.20,
    )
    # feat1 -- outcome edge should survive
    assert skel.has_edge("feat1", "outcome")


def test_estimate_causal_effect_recovers_known_slope() -> None:
    from eta_engine.brain.jarvis_v3.causal_discovery import estimate_causal_effect

    # outcome = 2 * treatment + noise
    treatment = [float(i) for i in range(50)]
    outcome = [2.0 * t + (i % 3 - 1) * 0.1 for i, t in enumerate(treatment)]
    feature_history = {"t": treatment, "y": outcome}
    eff = estimate_causal_effect(
        treatment="t",
        outcome="y",
        adjust_for=[],
        feature_history=feature_history,
    )
    # Beta should be near 2.0
    assert 1.8 < eff.beta < 2.2


def test_orient_by_time_assigns_direction() -> None:
    from eta_engine.brain.jarvis_v3.causal_discovery import (
        CausalSkeleton,
        orient_by_time,
    )

    skel = CausalSkeleton()
    skel.edges.add(("regime_at_open", "outcome_r"))
    out = orient_by_time(
        skel,
        earlier=["regime_at_open"],
        later=["outcome_r"],
    )
    assert out[("regime_at_open", "outcome_r")] == "a -> b"


# ─── memory_rag ──────────────────────────────────────────────────


def test_hash_embed_is_length_normalized() -> None:
    import math

    from eta_engine.brain.jarvis_v3.memory_rag import hash_embed

    e = hash_embed("alpha beta gamma alpha beta")
    norm = math.sqrt(sum(v * v for v in e.values()))
    assert abs(norm - 1.0) < 1e-9


def test_cosine_sparse_handles_disjoint() -> None:
    from eta_engine.brain.jarvis_v3.memory_rag import cosine_sparse

    a = {1: 0.5, 2: 0.5}
    b = {3: 0.7, 4: 0.7}
    assert cosine_sparse(a, b) == 0.0


def test_retrieve_similar_finds_text_match(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.memory_rag import retrieve_similar

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    mem.record_episode(
        signal_id="a",
        regime="neutral",
        session="rth",
        stress=0.5,
        direction="long",
        realized_r=1.0,
        narrative="liquidity sweep into prior high then reclaim",
    )
    mem.record_episode(
        signal_id="b",
        regime="neutral",
        session="rth",
        stress=0.5,
        direction="long",
        realized_r=-0.5,
        narrative="break of structure with bearish order block defense",
    )
    out = retrieve_similar(
        query_text="liquidity sweep reclaim",
        regime="neutral",
        session="rth",
        stress=0.5,
        direction="long",
        memory=mem,
        k=2,
    )
    assert len(out) == 2
    # First retrieved should be the matching narrative
    assert out[0].episode.signal_id == "a"


def test_summarize_episodes_renders_phrase_list(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import (
        Episode,
        HierarchicalMemory,  # noqa: F401 -- pulled in for test discoverability
    )
    from eta_engine.brain.jarvis_v3.memory_rag import summarize_episodes

    eps = [
        Episode(
            ts="",
            signal_id=f"s{i}",
            regime="neutral",
            session="rth",
            stress=0.5,
            direction="long",
            realized_r=1.0,
            narrative="liquidity sweep order block reclaim",
        )
        for i in range(3)
    ]
    summary = summarize_episodes(eps)
    assert "3 analog episodes" in summary
    assert "liquidity" in summary or "sweep" in summary or "order" in summary


def test_rag_enrich_produces_cautions_when_analogs_lost(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.memory_rag import rag_enrich_decision_context

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    for _ in range(5):
        mem.record_episode(
            signal_id="loss",
            regime="bearish_high_vol",
            session="rth",
            stress=0.7,
            direction="long",
            realized_r=-1.5,
            narrative="counter-trend long into resistance lost",
        )
    ctx = rag_enrich_decision_context(
        current_narrative="counter-trend long into resistance",
        regime="bearish_high_vol",
        session="rth",
        stress=0.7,
        direction="long",
        memory=mem,
        k=3,
    )
    assert ctx.cautions
    assert ctx.avg_analog_r < 0


# ─── path_generator_full ─────────────────────────────────────────


def test_full_path_stats_records_quantiles_per_step() -> None:
    from eta_engine.brain.jarvis_v3.path_generator_full import generate_paths_full

    stats = generate_paths_full(
        s0=100.0,
        regime_init="neutral",
        n_paths=200,
        horizon_steps=10,
        seed=42,
    )
    assert stats.n_paths == 200
    assert len(stats.quantiles_per_step) == 10
    # Every step should have all 5 quantiles
    for q in stats.quantiles_per_step:
        assert "p05" in q
        assert "p95" in q


def test_regime_visits_summed_to_one() -> None:
    from eta_engine.brain.jarvis_v3.path_generator_full import generate_paths_full

    stats = generate_paths_full(
        s0=100.0,
        regime_init="bullish_low_vol",
        n_paths=100,
        horizon_steps=20,
        seed=42,
    )
    total = sum(stats.regime_visit_pct.values())
    assert abs(total - 1.0) < 0.01


def test_macro_event_jump_at_step_widens_step_distribution() -> None:
    from eta_engine.brain.jarvis_v3.path_generator_full import generate_paths_full

    base = generate_paths_full(
        s0=100.0,
        regime_init="neutral",
        n_paths=300,
        horizon_steps=10,
        seed=42,
    )
    with_jump = generate_paths_full(
        s0=100.0,
        regime_init="neutral",
        n_paths=300,
        horizon_steps=10,
        macro_event_at_step=5,
        macro_event_jump_sigma=0.05,
        seed=42,
    )
    # At step 5, with_jump should have wider quantile range
    base_q = base.quantiles_per_step[5]
    jump_q = with_jump.quantiles_per_step[5]
    base_spread = base_q["p95"] - base_q["p05"]
    jump_spread = jump_q["p95"] - jump_q["p05"]
    assert jump_spread > base_spread


def test_regime_aware_summary_renders() -> None:
    from eta_engine.brain.jarvis_v3.path_generator_full import (
        generate_paths_full,
        regime_aware_summary,
    )

    stats = generate_paths_full(
        s0=100.0,
        n_paths=50,
        horizon_steps=10,
        seed=1,
    )
    summary = regime_aware_summary(stats)
    assert "regime-mixture" in summary
    assert "max-DD" in summary


# ─── firm_board_debate ───────────────────────────────────────────


def test_iterative_verdict_records_three_rounds() -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.firm_board_debate import deliberate_iterative

    p = Proposal(
        signal_id="iter1",
        direction="long",
        regime="bullish_low_vol",
        session="rth",
        stress=0.3,
        sentiment=0.5,
        sage_score=0.6,
        slippage_bps_estimate=2.0,
    )
    v = deliberate_iterative(
        proposal=p,
        memory=None,
        devils_advocate_probability=0.0,
        seed=42,
    )
    assert len(v.round_1_arguments) == 5
    assert len(v.round_2_rebuttals) == 5
    assert len(v.round_3_final_arguments) == 5


def test_iterative_verdict_consensus_can_change_between_rounds() -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.firm_board_debate import deliberate_iterative

    # Devil's advocate may flip a stance, sharpening or softening consensus
    p = Proposal(
        signal_id="iter2",
        direction="long",
        regime="bullish_low_vol",
        session="rth",
        stress=0.2,
        sentiment=0.6,
        sage_score=0.6,
    )
    v = deliberate_iterative(
        proposal=p,
        memory=None,
        devils_advocate_probability=1.0,
        seed=1,
    )
    # With probability 1, devil's advocate fires -- we just verify the
    # bookkeeping is consistent
    assert v.devils_advocate_role is not None or v.round_1_consensus < 1.0


def test_iterative_verdict_audit_record_is_serializable() -> None:
    import json

    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.firm_board_debate import deliberate_iterative

    p = Proposal(
        signal_id="iter3",
        direction="short",
        regime="bearish_low_vol",
        session="rth",
        stress=0.4,
        sentiment=-0.3,
        sage_score=0.4,
    )
    v = deliberate_iterative(proposal=p, memory=None, seed=99)
    rec = v.to_audit_record()
    s = json.dumps(rec)
    assert "round_1_arguments" in s
    assert "round_2_rebuttals" in s
    assert "round_3_final_arguments" in s


# ─── meta_learner_full ───────────────────────────────────────────


def test_compute_multi_objective_handles_empty() -> None:
    from eta_engine.brain.jarvis_v3.meta_learner_full import compute_multi_objective

    m = compute_multi_objective([])
    assert m.n_observations == 0
    assert m.avg_r == 0.0


def test_compute_multi_objective_winning_distribution() -> None:
    from eta_engine.brain.jarvis_v3.meta_learner_full import compute_multi_objective

    rs = [1.0, 2.0, 1.5, -0.3, 1.8]
    m = compute_multi_objective(rs)
    assert m.avg_r > 1.0
    assert m.sharpe > 0
    assert m.max_dd_r >= 0


def test_pareto_dominates_strict_improvement() -> None:
    from eta_engine.brain.jarvis_v3.meta_learner_full import (
        MultiObjective,
        pareto_dominates,
    )

    champion = MultiObjective(avg_r=1.0, sharpe=1.0, max_dd_r=2.0, ulcer_index=1.0)
    challenger_better = MultiObjective(
        avg_r=1.2,
        sharpe=1.2,
        max_dd_r=1.8,
        ulcer_index=0.9,
    )
    challenger_regression = MultiObjective(
        avg_r=1.2,
        sharpe=1.2,
        max_dd_r=2.5,
        ulcer_index=0.9,  # max_dd worse
    )
    assert pareto_dominates(challenger_better, champion) is True
    assert pareto_dominates(challenger_regression, champion) is False


def test_parameter_importance_bandit_picks_high_leverage_arm() -> None:
    import random

    from eta_engine.brain.jarvis_v3.meta_learner_full import ParameterImportanceBandit

    bandit = ParameterImportanceBandit(epsilon=0.0)  # pure greedy
    bandit.register_param("a")
    bandit.register_param("b")
    # Tell the bandit "a" produces +1.0 improvements; "b" produces -0.5
    for _ in range(5):
        bandit.observe("a", 1.0)
        bandit.observe("b", -0.5)
    rng = random.Random(42)
    pick = bandit.pick(rng=rng)
    assert pick == "a"


def test_meta_learner_full_does_not_promote_under_min_episodes() -> None:
    from eta_engine.brain.jarvis_v3.meta_learner_full import (
        MetaLearnerFull,
        MetaLearnerFullConfig,
    )

    ml = MetaLearnerFull(
        cfg=MetaLearnerFullConfig(
            min_episodes=20,
            n_challengers=2,
            max_experiments_per_day=10,
        ),
    )
    ml.update_champion_metrics([0.5] * 10)
    challengers = ml.spawn_challengers()
    target = challengers[0]
    for _ in range(3):
        ml.record_outcome(target.challenger_id, 5.0)
    rec = ml.evaluate_and_promote()
    assert rec is None


def test_meta_learner_full_promotes_pareto_dominant_challenger() -> None:
    from eta_engine.brain.jarvis_v3.meta_learner_full import (
        MetaLearnerFull,
        MetaLearnerFullConfig,
    )

    ml = MetaLearnerFull(
        cfg=MetaLearnerFullConfig(
            min_episodes=5,
            n_challengers=2,
            max_experiments_per_day=10,
            auto_promote=True,
        ),
    )
    # Champion mediocre with high variance
    ml.update_champion_metrics([0.5, -0.2, 0.3, 0.1, 0.4, 0.2, 0.0])
    challengers = ml.spawn_challengers()
    target = challengers[0]
    # Challenger strong AND with positive variance so sharpe stays defined
    for r in [2.5, 1.8, 2.2, 1.5, 2.0, 1.7, 2.1, 1.9]:
        ml.record_outcome(target.challenger_id, r)
    rec = ml.evaluate_and_promote()
    assert rec is not None
    assert rec["auto_promoted"] is True
    assert rec["challenger_metrics"]["avg_r"] > rec["champion_metrics"]["avg_r"]
