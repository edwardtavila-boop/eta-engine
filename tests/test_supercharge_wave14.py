"""Tests for wave-14 (JARVIS explains himself).

Covers:
  * narrative_generator.py    -- prose rendering of verdicts
  * operator_coach.py         -- override-pattern Bayesian learner
  * skill_health_registry.py  -- external dep health tracking
  * daily_brief.py            -- end-of-day operator summary
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─── narrative_generator.py ───────────────────────────────────────


def _make_verdict(**overrides):
    from eta_engine.brain.jarvis_v3.intelligence import ConsolidatedVerdict

    defaults = dict(
        ts="2026-04-27T15:00:00+00:00",
        request_id="r1",
        subsystem="MNQ_BOT",
        action="ORDER_PLACE",
        base_verdict="APPROVED",
        base_reason="ok",
        final_verdict="APPROVED",
        final_size_multiplier=1.0,
        confidence=0.7,
        operator_override_level="NORMAL",
        intelligence_enabled=True,
        rag_summary="3 analogs avg +0.8R",
        rag_cautions=[],
        rag_boosts=["analog winner"],
        causal_score=0.4,
        causal_reason="strong support",
        world_model_best_action="approve_full",
        world_model_expected_r=1.2,
        firm_board_consensus=0.7,
        firm_board_devils_advocate=None,
        layer_errors=[],
    )
    defaults.update(overrides)
    return ConsolidatedVerdict(**defaults)


def test_narrative_terse_returns_one_sentence() -> None:
    from eta_engine.brain.jarvis_v3.narrative_generator import (
        verdict_to_narrative,
    )

    v = _make_verdict()
    out = verdict_to_narrative(v, verbosity="terse")
    assert "Approved" in out
    # Terse should be a single line
    assert "\n" not in out


def test_narrative_terse_blocks_on_hard_pause() -> None:
    from eta_engine.brain.jarvis_v3.narrative_generator import (
        verdict_to_narrative,
    )

    v = _make_verdict(
        operator_override_level="HARD_PAUSE",
        final_verdict="DENIED",
        final_size_multiplier=0.0,
    )
    out = verdict_to_narrative(v, verbosity="terse")
    assert "BLOCKED" in out


def test_narrative_standard_mentions_consensus_and_causal() -> None:
    from eta_engine.brain.jarvis_v3.narrative_generator import (
        verdict_to_narrative,
    )

    v = _make_verdict(firm_board_consensus=0.8, causal_score=0.5)
    out = verdict_to_narrative(v, verbosity="standard")
    assert "consensus" in out.lower()
    assert "causal" in out.lower()


def test_narrative_verbose_includes_all_layers() -> None:
    from eta_engine.brain.jarvis_v3.narrative_generator import (
        verdict_to_narrative,
    )

    v = _make_verdict()
    out = verdict_to_narrative(v, verbosity="verbose")
    assert "DECISION" in out
    assert "Firm-board consensus" in out
    assert "World model" in out
    assert "RAG" in out


# ─── operator_coach.py ────────────────────────────────────────────


def test_operator_coach_initial_advice_is_auto_proceed(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.operator_coach import OperatorCoach

    coach = OperatorCoach(state_path=tmp_path / "coach.json")
    advice = coach.should_defer_to_operator(
        regime="bullish_low_vol",
        session="rth",
        action="ORDER_PLACE",
    )
    assert advice.recommendation == "auto_proceed"
    assert advice.suggested_size_shrink == 1.0


def test_operator_coach_records_overrides_and_advises_softening(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.operator_coach import OperatorCoach

    coach = OperatorCoach(state_path=tmp_path / "coach.json")
    # Record a series: 50% override rate over 10 observations
    for i in range(10):
        coach.record_outcome(
            regime="bearish_high_vol",
            session="overnight",
            action="ORDER",
            was_overridden=(i % 2 == 0),
        )
    advice = coach.should_defer_to_operator(
        regime="bearish_high_vol",
        session="overnight",
        action="ORDER",
    )
    assert advice.n_observations >= 5
    # Beta(6,6) -> mean 0.5, recommend soften
    assert advice.recommendation == "soften"
    assert advice.suggested_size_shrink < 1.0


def test_operator_coach_high_override_rate_escalates(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.operator_coach import OperatorCoach

    coach = OperatorCoach(state_path=tmp_path / "coach.json")
    # 9 overrides out of 10 -> high
    for i in range(10):
        coach.record_outcome(
            regime="neutral",
            session="rth",
            action="ORDER",
            was_overridden=(i < 9),
        )
    advice = coach.should_defer_to_operator(
        regime="neutral",
        session="rth",
        action="ORDER",
    )
    assert advice.recommendation == "escalate"
    assert advice.override_probability > 0.6


def test_operator_coach_persists_across_instances(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.operator_coach import OperatorCoach

    path = tmp_path / "coach.json"
    c1 = OperatorCoach(state_path=path)
    for _ in range(5):
        c1.record_outcome(
            regime="bull",
            session="rth",
            action="ORDER",
            was_overridden=True,
        )
    c2 = OperatorCoach(state_path=path)
    advice = c2.should_defer_to_operator(
        regime="bull",
        session="rth",
        action="ORDER",
    )
    # 5 overrides, 0 acceptances -> Beta(6,1) mean ~0.857 -> escalate
    assert advice.recommendation in {"soften", "escalate"}


def test_operator_coach_report_sorted_by_override_prob(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.operator_coach import OperatorCoach

    coach = OperatorCoach(state_path=tmp_path / "coach.json")
    # High override rate cell
    for _ in range(8):
        coach.record_outcome(
            regime="A",
            session="rth",
            action="ORDER",
            was_overridden=True,
        )
    # Low override rate cell
    for _ in range(8):
        coach.record_outcome(
            regime="B",
            session="rth",
            action="ORDER",
            was_overridden=False,
        )
    rep = coach.report()
    assert rep[0]["regime"] == "A"
    assert rep[0]["override_probability"] > rep[-1]["override_probability"]


# ─── skill_health_registry.py ─────────────────────────────────────


def test_skill_registry_register_and_record(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.skill_health_registry import SkillRegistry

    reg = SkillRegistry(state_path=tmp_path / "skill.json")
    reg.register_skill("ibkr_data", kind="market_data", target_latency_ms=200)
    reg.record_call("ibkr_data", success=True, latency_ms=150)
    reg.record_call("ibkr_data", success=True, latency_ms=180)
    h = reg.health("ibkr_data")
    assert h is not None
    assert h.status.value == "HEALTHY"
    assert h.n_calls == 2


def test_skill_registry_marks_degraded_on_high_error_rate(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.skill_health_registry import SkillRegistry

    reg = SkillRegistry(state_path=tmp_path / "skill.json")
    reg.register_skill("flaky", target_latency_ms=200)
    # Mixed: 2/10 errors -> 20% error rate -> DEGRADED
    for i in range(10):
        reg.record_call(
            "flaky",
            success=(i >= 2),
            latency_ms=100,
            error_msg="" if i >= 2 else "boom",
        )
    h = reg.health("flaky")
    assert h is not None
    assert h.status.value == "DEGRADED"


def test_skill_registry_marks_unavailable_on_consecutive_failures(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.skill_health_registry import SkillRegistry

    reg = SkillRegistry(state_path=tmp_path / "skill.json")
    reg.register_skill("dead", target_latency_ms=200)
    for _ in range(6):
        reg.record_call("dead", success=False, error_msg="connection refused")
    h = reg.health("dead")
    assert h is not None
    assert h.status.value == "UNAVAILABLE"
    assert reg.is_available("dead") is False


def test_skill_registry_persists_state(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.skill_health_registry import SkillRegistry

    path = tmp_path / "skill.json"
    r1 = SkillRegistry(state_path=path)
    r1.register_skill("persist_test", target_latency_ms=100)
    for _ in range(10):
        r1.record_call("persist_test", success=True, latency_ms=50)
    r1.force_save()
    r2 = SkillRegistry(state_path=path)
    h = r2.health("persist_test")
    assert h is not None
    assert h.n_calls == 10


def test_skill_registry_degraded_or_unavailable_filter(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.skill_health_registry import SkillRegistry

    reg = SkillRegistry(state_path=tmp_path / "skill.json")
    reg.register_skill("good", target_latency_ms=100)
    reg.register_skill("bad", target_latency_ms=100)
    for _ in range(10):
        reg.record_call("good", success=True, latency_ms=50)
    for _ in range(10):
        reg.record_call("bad", success=False, error_msg="x")
    bad_skills = reg.degraded_or_unavailable()
    bad_names = {h.name for h in bad_skills}
    assert "bad" in bad_names
    assert "good" not in bad_names


# ─── daily_brief.py ───────────────────────────────────────────────


def test_daily_brief_renders_with_empty_logs(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.daily_brief import generate_daily_brief

    brief = generate_daily_brief(
        n_hours_back=24,
        output_dir=tmp_path / "briefs",
        state_dir=tmp_path / "jarvis_intel",
        auto_persist=False,
    )
    assert brief.n_verdicts == 0
    assert brief.n_trades == 0
    md = brief.to_markdown()
    assert "JARVIS Daily Brief" in md


def test_daily_brief_to_dict_serializable(tmp_path: Path) -> None:
    import json

    from eta_engine.brain.jarvis_v3.daily_brief import generate_daily_brief

    brief = generate_daily_brief(
        n_hours_back=24,
        output_dir=tmp_path / "briefs",
        state_dir=tmp_path / "jarvis_intel",
        auto_persist=False,
    )
    s = json.dumps(brief.to_dict())
    assert "headline" in s


def test_daily_brief_persists_md_and_json(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.daily_brief import generate_daily_brief

    out = tmp_path / "briefs"
    generate_daily_brief(
        n_hours_back=24,
        output_dir=out,
        state_dir=tmp_path / "jarvis_intel",
        auto_persist=True,
    )
    files_md = list(out.glob("*.md"))
    files_json = list(out.glob("*.json"))
    assert files_md
    assert files_json
