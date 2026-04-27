"""Integration test for jarvis_full: every wave wired together."""
from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from pathlib import Path


def _stub_request(**overrides):
    req = MagicMock()
    req.request_id = overrides.get("request_id", "r1")
    req.subsystem = overrides.get("subsystem", "MNQ_BOT")
    req.action_type = overrides.get("action_type", "ORDER_PLACE")
    req.payload = overrides.get("payload", {
        "regime": "bullish_low_vol", "session": "rth",
        "stress": 0.3, "direction": "long",
        "sentiment": 0.4, "sage_score": 0.5,
        "slippage_bps_estimate": 2.0,
    })
    return req


def _stub_response(verdict="APPROVED"):
    r = MagicMock()
    r.verdict = verdict
    r.reason_code = "ok"
    r.size_cap_qty = None
    return r


def test_jarvis_full_consult_runs_all_layers(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.intelligence import (
        IntelligenceConfig,
        JarvisIntelligence,
    )
    from eta_engine.brain.jarvis_v3.jarvis_full import JarvisFull
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.operator_coach import OperatorCoach
    from eta_engine.brain.jarvis_v3.skill_health_registry import SkillRegistry
    from eta_engine.brain.jarvis_v3.thesis_tracker import ThesisTracker

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    # Seed memory so RAG / world model / OOD have data
    for r in [1.0, 1.5, 0.8, -0.3, 1.2]:
        mem.record_episode(
            signal_id=f"s{r}", regime="bullish_low_vol", session="rth",
            stress=0.3, direction="long", realized_r=r,
            narrative="EMA stack confluence",
        )

    admin = MagicMock()
    admin.request_approval.return_value = _stub_response("APPROVED")
    intel = JarvisIntelligence(
        admin=admin, memory=mem,
        cfg=IntelligenceConfig(enable_intelligence=True),
        verdict_log=tmp_path / "verdicts.jsonl",
    )
    full = JarvisFull(
        intelligence=intel, memory=mem,
        operator_coach=OperatorCoach(state_path=tmp_path / "coach.json"),
        skill_registry=SkillRegistry(state_path=tmp_path / "skill.json"),
        thesis_tracker=ThesisTracker(
            theses_path=tmp_path / "theses.json",
            breach_log_path=tmp_path / "breaches.jsonl",
        ),
    )

    verdict = full.consult(
        _stub_request(),
        current_narrative="EMA stack aligned, sage approved",
    )

    assert verdict.consolidated is not None
    assert verdict.narrative_terse
    assert verdict.narrative_standard
    # Every layer ran (or recorded an error -- but didn't crash)
    assert isinstance(verdict.premortem_kill_prob, float)
    assert isinstance(verdict.ood_score, float)
    assert isinstance(verdict.final_size_multiplier, float)


def test_jarvis_full_blocked_when_admin_denies(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.intelligence import (
        IntelligenceConfig,
        JarvisIntelligence,
    )
    from eta_engine.brain.jarvis_v3.jarvis_full import JarvisFull
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    admin = MagicMock()
    admin.request_approval.return_value = _stub_response("DENIED")
    intel = JarvisIntelligence(
        admin=admin, memory=mem,
        cfg=IntelligenceConfig(enable_intelligence=False),
        verdict_log=tmp_path / "verdicts.jsonl",
    )
    full = JarvisFull(intelligence=intel, memory=mem)
    verdict = full.consult(_stub_request())
    assert verdict.is_blocked() is True
    assert verdict.final_size_multiplier == 0.0


def test_jarvis_full_to_dict_serializable(tmp_path: Path) -> None:
    import json

    from eta_engine.brain.jarvis_v3.intelligence import (
        IntelligenceConfig,
        JarvisIntelligence,
    )
    from eta_engine.brain.jarvis_v3.jarvis_full import JarvisFull
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    admin = MagicMock()
    admin.request_approval.return_value = _stub_response("APPROVED")
    intel = JarvisIntelligence(
        admin=admin, memory=mem,
        cfg=IntelligenceConfig(enable_intelligence=False),
        verdict_log=tmp_path / "verdicts.jsonl",
    )
    full = JarvisFull(intelligence=intel, memory=mem)
    verdict = full.consult(_stub_request())
    s = json.dumps(verdict.to_dict(), default=str)
    assert "consolidated" in s
    assert "final_size_multiplier" in s


def test_jarvis_full_health_helper_returns_dict() -> None:
    """The convenience health() call should not raise."""
    from eta_engine.brain.jarvis_v3.intelligence import JarvisIntelligence
    from eta_engine.brain.jarvis_v3.jarvis_full import JarvisFull
    admin = MagicMock()
    intel = JarvisIntelligence(admin=admin, memory=None)
    full = JarvisFull(intelligence=intel)
    h = full.health()
    assert "overall_status" in h
