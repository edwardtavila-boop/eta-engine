"""Tests for wave-8 (advanced AI capabilities, 2026-04-27).

Covers the 6 modules shipped to address the user-supplied advanced
AI/ML upgrade list:

  * #3 Hierarchical memory (memory_hierarchy.py)
  * #2 Causal layer (causal_layer.py)
  * #6 Firm-board debate (firm_board.py)
  * #7 Meta-learner (meta_learner.py)
  * #1 World model rollouts (world_model.py)
  * #4 Hybrid path generator (path_generator.py)

Quantum (#5) intentionally skipped as premature.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

# ─── Hierarchical memory ──────────────────────────────────────────


def test_episode_feature_vector_is_dimension_stable() -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import Episode
    e1 = Episode(ts="", signal_id="a", regime="bullish_low_vol", session="rth",
                 stress=0.3, direction="long", realized_r=1.0)
    e2 = Episode(ts="", signal_id="b", regime="bearish_high_vol", session="overnight",
                 stress=0.9, direction="short", realized_r=-1.0)
    assert len(e1.feature_vector()) == len(e2.feature_vector()) == 4


def test_memory_records_and_recalls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import memory_hierarchy as mh
    monkeypatch.setattr(mh, "EPISODIC_PATH", tmp_path / "ep.jsonl")
    monkeypatch.setattr(mh, "SEMANTIC_PATH", tmp_path / "sem.json")
    monkeypatch.setattr(mh, "PROCEDURAL_PATH", tmp_path / "proc.jsonl")

    mem = mh.HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    mem.record_episode(
        signal_id="s1", regime="bullish_low_vol", session="rth",
        stress=0.3, direction="long", realized_r=1.5,
    )
    mem.record_episode(
        signal_id="s2", regime="bullish_low_vol", session="rth",
        stress=0.3, direction="long", realized_r=2.0,
    )
    similar = mem.recall_similar(
        regime="bullish_low_vol", session="rth", stress=0.3,
        direction="long", k=5,
    )
    assert len(similar) == 2


def test_memory_semantic_aggregation(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    for r in [1.0, -0.5, 2.0]:
        mem.record_episode(
            signal_id=f"s{r}", regime="bullish_low_vol", session="rth",
            stress=0.3, direction="long", realized_r=r,
        )
    fact = mem.lookup_pattern(
        regime="bullish_low_vol", session="rth", direction="long",
    )
    assert fact is not None
    assert fact.n_episodes == 3
    assert abs(fact.avg_r - (1.0 - 0.5 + 2.0) / 3.0) < 1e-9
    assert fact.win_rate == 2 / 3


def test_memory_procedural_lineage(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    mem.record_procedural_version(
        version_id="v1", parent_id=None, params={"x": 1}, realized_metric=0.5,
    )
    mem.record_procedural_version(
        version_id="v2", parent_id="v1", params={"x": 2}, realized_metric=0.8,
    )
    mem.record_procedural_version(
        version_id="v3", parent_id="v2", params={"x": 3}, realized_metric=1.1,
    )
    chain = mem.procedural_lineage("v3")
    assert [v.version_id for v in chain] == ["v3", "v2", "v1"]
    best = mem.best_procedural_version()
    assert best is not None
    assert best.version_id == "v3"


def test_memory_persists_across_instances(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    paths = dict(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    m1 = HierarchicalMemory(**paths)
    m1.record_episode(
        signal_id="x", regime="neutral", session="rth", stress=0.5,
        direction="long", realized_r=0.5,
    )
    m2 = HierarchicalMemory(**paths)
    similar = m2.recall_similar(
        regime="neutral", session="rth", stress=0.5, k=5,
    )
    assert len(similar) == 1
    assert similar[0].signal_id == "x"


# ─── Causal layer ─────────────────────────────────────────────────


def test_granger_score_returns_zero_for_short_series() -> None:
    from eta_engine.brain.jarvis_v3.causal_layer import granger_score
    assert granger_score([1.0, 2.0], [1.0, 2.0]) == 0.0


def test_granger_score_picks_up_lagged_relationship() -> None:
    from eta_engine.brain.jarvis_v3.causal_layer import granger_score
    # Cause leads outcome by 1 step
    cause = [0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0]
    outcome = [0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0]
    s = granger_score(cause, outcome, lag=1)
    # Should be non-trivial (negative or positive but not zero)
    assert s != 0.0


def test_intervention_score_low_when_no_data(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.causal_layer import intervention_score
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    score, n = intervention_score(
        proposed_action="approve_full", regime="bullish_low_vol",
        session="rth", direction="long", memory=mem,
    )
    assert score == 0.0
    assert n == 0


def test_intervention_score_uses_journal_when_present(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.causal_layer import intervention_score
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    for r in [1.5, 2.0, 1.0, 1.8, 2.2, 1.6]:
        mem.record_episode(
            signal_id=f"s{r}", regime="bullish_low_vol", session="rth",
            stress=0.3, direction="long", realized_r=r,
            extra={"action": "approve_full"},
        )
    score, n = intervention_score(
        proposed_action="approve_full", regime="bullish_low_vol",
        session="rth", direction="long", memory=mem,
    )
    assert n == 6
    # Avg R = ~1.68; score = 1.68/2.0 = 0.84 capped at 1.0
    assert score > 0.5


def test_score_causal_support_combines_legs(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.causal_layer import score_causal_support
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    for _ in range(10):
        mem.record_episode(
            signal_id="s", regime="bullish_low_vol", session="rth",
            stress=0.3, direction="long", realized_r=1.5,
            extra={"action": "approve_full"},
        )
    ev = score_causal_support(
        signal_features={"sentiment": 0.4},
        proposed_action="approve_full",
        regime="bullish_low_vol", session="rth", direction="long",
        memory=mem,
    )
    assert ev.n_supporting_episodes == 10
    assert ev.intervention_score > 0.5
    assert ev.score > 0.3


def test_adjusted_outcome_strips_linear_confounder() -> None:
    from eta_engine.brain.jarvis_v3.causal_layer import adjusted_outcome
    # Outcome perfectly tracks confounder -> residuals near zero
    confounder = [1.0, 2.0, 3.0, 4.0, 5.0]
    outcome = [2.0, 4.0, 6.0, 8.0, 10.0]   # = 2 * confounder
    residuals = adjusted_outcome(raw_outcomes=outcome, confounders=confounder)
    assert all(abs(r) < 1e-6 for r in residuals)


# ─── Firm board ────────────────────────────────────────────────────


def test_firm_board_unanimous_support_approves_full() -> None:
    from eta_engine.brain.jarvis_v3.firm_board import (
        FinalAction,
        Proposal,
        deliberate,
    )
    p = Proposal(
        signal_id="s1", direction="long", regime="bullish_low_vol",
        session="rth", stress=0.2, sentiment=0.6, sage_score=0.7,
        slippage_bps_estimate=2.0,
    )
    v = deliberate(proposal=p, memory=None)
    assert v.final_action in {FinalAction.APPROVE_FULL, FinalAction.APPROVE_HALF}


def test_firm_board_high_stress_triggers_risk_veto() -> None:
    from eta_engine.brain.jarvis_v3.firm_board import (
        FinalAction,
        Proposal,
        deliberate,
    )
    p = Proposal(
        signal_id="s2", direction="long", regime="bullish_low_vol",
        session="overnight", stress=0.85, sentiment=0.3, sage_score=0.3,
        slippage_bps_estimate=12.0,
    )
    v = deliberate(proposal=p, memory=None)
    assert v.final_action == FinalAction.DENY


def test_firm_board_consensus_score_in_unit_interval() -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal, deliberate
    p = Proposal(
        signal_id="s3", direction="long", regime="neutral", session="rth",
        stress=0.4, sentiment=0.0, sage_score=0.0,
    )
    v = deliberate(proposal=p, memory=None)
    assert 0.0 <= v.consensus <= 1.0


def test_firm_board_uses_memory_when_supplied(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.firm_board import (
        Proposal,
        Role,
        deliberate,
    )
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    for _ in range(8):
        mem.record_episode(
            signal_id="hist", regime="bullish_low_vol", session="rth",
            stress=0.3, direction="long", realized_r=2.0,
        )
    p = Proposal(
        signal_id="s4", direction="long", regime="bullish_low_vol",
        session="rth", stress=0.3, sentiment=0.4, sage_score=0.5,
        slippage_bps_estimate=2.0,
    )
    v = deliberate(proposal=p, memory=mem)
    auditor_arg = next(a for a in v.arguments if a.role == Role.AUDITOR)
    # Auditor should have found supporting analogs
    assert auditor_arg.stance == "support"


def test_firm_board_audit_record_is_serializable() -> None:
    import json

    from eta_engine.brain.jarvis_v3.firm_board import Proposal, deliberate
    p = Proposal(
        signal_id="s5", direction="short", regime="bearish_low_vol",
        session="rth", stress=0.4, sentiment=-0.3, sage_score=0.4,
        slippage_bps_estimate=4.0,
    )
    v = deliberate(proposal=p, memory=None)
    rec = v.to_audit_record()
    s = json.dumps(rec)
    assert "RESEARCHER" in s
    assert "RISK_COMMITTEE" in s


# ─── Meta-learner ─────────────────────────────────────────────────


def test_mutate_changes_one_param_and_clamps() -> None:
    from eta_engine.brain.jarvis_v3.meta_learner import (
        CandidateConfig,
        ParamBounds,
        mutate,
    )
    cfg = CandidateConfig()
    new_cfg = mutate(cfg, n_mutations=1)
    # At least one field differs
    differs = sum(
        1 for k in cfg.to_dict()
        if abs(cfg.to_dict()[k] - new_cfg.to_dict()[k]) > 1e-9
    )
    assert differs >= 1
    # All values stay in their bounds
    bounds = ParamBounds()
    for k, v in new_cfg.to_dict().items():
        lo, hi = getattr(bounds, k)
        assert lo <= v <= hi


def test_meta_learner_proposes_when_challenger_beats_champion(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.meta_learner import (
        MetaLearner,
        MetaLearnerConfig,
    )
    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    ml = MetaLearner(
        cfg=MetaLearnerConfig(
            promotion_margin_r=0.10, min_episodes=5,
            n_challengers=2, auto_promote=False,
        ),
        champion_path=tmp_path / "champ.json",
    )
    challengers = ml.spawn_challengers()
    assert len(challengers) == 2
    # Feed strong shadow returns to one challenger
    target = challengers[0]
    for _ in range(10):
        ml.record_shadow_outcome(target.challenger_id, realized_r=2.0)
    promoted = ml.evaluate_and_promote(memory=mem, champion_avg_r=0.0)
    assert promoted is not None
    assert promoted.parent_id == "v0_genesis"


def test_meta_learner_does_not_promote_under_min_episodes(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.meta_learner import (
        MetaLearner,
        MetaLearnerConfig,
    )
    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    ml = MetaLearner(
        cfg=MetaLearnerConfig(
            promotion_margin_r=0.10, min_episodes=20,
            n_challengers=2, auto_promote=False,
        ),
        champion_path=tmp_path / "champ.json",
    )
    challengers = ml.spawn_challengers()
    target = challengers[0]
    # Only feed 3 observations -- below min_episodes=20
    for _ in range(3):
        ml.record_shadow_outcome(target.challenger_id, realized_r=5.0)
    promoted = ml.evaluate_and_promote(memory=mem, champion_avg_r=0.0)
    assert promoted is None


def test_meta_learner_auto_promote_swaps_champion(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.meta_learner import (
        MetaLearner,
        MetaLearnerConfig,
    )
    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    ml = MetaLearner(
        cfg=MetaLearnerConfig(
            promotion_margin_r=0.10, min_episodes=5,
            n_challengers=2, auto_promote=True,
        ),
        champion_path=tmp_path / "champ.json",
    )
    prior_champion_id = ml.champion_id()
    challengers = ml.spawn_challengers()
    target = challengers[0]
    for _ in range(8):
        ml.record_shadow_outcome(target.challenger_id, realized_r=1.5)
    promoted = ml.evaluate_and_promote(memory=mem, champion_avg_r=0.0)
    assert promoted is not None
    assert ml.champion_id() != prior_champion_id


# ─── World model ──────────────────────────────────────────────────


def test_encode_state_round_trip_describes() -> None:
    from eta_engine.brain.jarvis_v3.world_model import describe_state, encode_state
    s = encode_state(regime="bullish_low_vol", session="rth", stress=0.3)
    label = describe_state(s)
    assert "bullish_low_vol" in label
    assert "rth" in label
    assert "low_vol" in label


def test_dream_returns_zeroed_report_when_no_episodes(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.world_model import dream, encode_state
    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    s = encode_state(regime="bullish_low_vol", session="rth", stress=0.3)
    rep = dream(current_state=s, n_paths=10, horizon=5, memory=mem)
    # No episodes -> rewards always 0 -> all paths terminal at 0
    assert rep.n_paths == 10
    assert rep.avg_terminal_r == 0.0


def test_dream_picks_up_positive_regime_when_history_is_positive(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.world_model import dream, encode_state
    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    # Plenty of winning episodes in this regime
    for _ in range(40):
        mem.record_episode(
            signal_id="w", regime="bullish_low_vol", session="rth",
            stress=0.3, direction="long", realized_r=1.0,
        )
    s = encode_state(regime="bullish_low_vol", session="rth", stress=0.3)
    rep = dream(current_state=s, n_paths=50, horizon=5, memory=mem)
    assert rep.avg_terminal_r > 0
    assert rep.pct_paths_profitable > 0.7


def test_transition_table_fits_from_episodes(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.world_model import TransitionTable
    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    # Two regimes alternating -> transition table picks up the pattern
    for i in range(20):
        mem.record_episode(
            signal_id=f"s{i}",
            regime="bullish_low_vol" if i % 2 == 0 else "bearish_low_vol",
            session="rth", stress=0.3, direction="long",
            realized_r=0.5 if i % 2 == 0 else -0.5,
        )
    table = TransitionTable()
    table.fit_from_episodes(mem._episodes)
    # At least one transition observed
    assert table.transitions
    assert table.rewards_by_state


# ─── Path generator ───────────────────────────────────────────────


def test_generate_paths_returns_correct_shape() -> None:
    from eta_engine.brain.jarvis_v3.path_generator import generate_paths
    stats = generate_paths(
        s0=21450.0, n_paths=100, horizon_steps=20,
        regime="neutral", seed=42,
    )
    assert stats.n_paths == 100
    assert stats.horizon_steps == 20
    assert stats.s0 == 21450.0
    assert len(stats.sample_paths) == 5
    assert all(len(p) == 20 for p in stats.sample_paths)


def test_generate_paths_bullish_regime_drifts_up_more_than_bearish() -> None:
    from eta_engine.brain.jarvis_v3.path_generator import generate_paths
    bull = generate_paths(
        s0=100.0, n_paths=400, horizon_steps=60,
        regime="bullish_low_vol", seed=1,
    )
    bear = generate_paths(
        s0=100.0, n_paths=400, horizon_steps=60,
        regime="bearish_low_vol", seed=1,
    )
    # Median terminal % should be higher in bullish
    assert bull.median_terminal_pct > bear.median_terminal_pct


def test_generate_paths_high_vol_widens_tails() -> None:
    from eta_engine.brain.jarvis_v3.path_generator import generate_paths
    high = generate_paths(
        s0=100.0, n_paths=400, horizon_steps=60,
        regime="bullish_high_vol", seed=1,
    )
    low = generate_paths(
        s0=100.0, n_paths=400, horizon_steps=60,
        regime="bullish_low_vol", seed=1,
    )
    high_spread = high.p95_terminal_pct - high.p05_terminal_pct
    low_spread = low.p95_terminal_pct - low.p05_terminal_pct
    assert high_spread > low_spread


def test_summarize_paths_renders_human_readable() -> None:
    from eta_engine.brain.jarvis_v3.path_generator import (
        generate_paths,
        summarize_paths,
    )
    stats = generate_paths(s0=100.0, n_paths=50, horizon_steps=10, seed=1)
    summary = summarize_paths(stats)
    assert "median terminal" in summary
    assert "max-DD" in summary
