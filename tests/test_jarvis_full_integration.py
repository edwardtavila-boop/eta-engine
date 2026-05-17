"""Integration test for jarvis_full: every wave wired together."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _redirect_default_trace_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep full-consult tests from appending mock records to live Jarvis trace."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    monkeypatch.setattr(trace_emitter, "DEFAULT_TRACE_PATH", tmp_path / "jarvis_trace.jsonl")


def _stub_request(**overrides):
    req = MagicMock()
    req.request_id = overrides.get("request_id", "r1")
    req.subsystem = overrides.get("subsystem", "MNQ_BOT")
    req.action_type = overrides.get("action_type", "ORDER_PLACE")
    req.payload = overrides.get(
        "payload",
        {
            "regime": "bullish_low_vol",
            "session": "rth",
            "stress": 0.3,
            "direction": "long",
            "sentiment": 0.4,
            "sage_score": 0.5,
            "slippage_bps_estimate": 2.0,
        },
    )
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
            signal_id=f"s{r}",
            regime="bullish_low_vol",
            session="rth",
            stress=0.3,
            direction="long",
            realized_r=r,
            narrative="EMA stack confluence",
        )

    admin = MagicMock()
    admin.request_approval.return_value = _stub_response("APPROVED")
    intel = JarvisIntelligence(
        admin=admin,
        memory=mem,
        cfg=IntelligenceConfig(enable_intelligence=True),
        verdict_log=tmp_path / "verdicts.jsonl",
    )
    full = JarvisFull(
        intelligence=intel,
        memory=mem,
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
        admin=admin,
        memory=mem,
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
        admin=admin,
        memory=mem,
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


def test_consult_sage_for_request_builds_immutable_context_with_live_telemetry(monkeypatch) -> None:
    import eta_engine.brain.jarvis_v3.sage as sage_pkg
    from eta_engine.brain.jarvis_v3.intelligence import JarvisIntelligence
    from eta_engine.brain.jarvis_v3.jarvis_full import JarvisFull
    from eta_engine.brain.jarvis_v3.sage.base import Bias, SageReport, SchoolVerdict

    captured: dict[str, object] = {}

    def fake_consult(ctx):
        captured["ctx"] = ctx
        return SageReport(
            per_school={
                "sentiment_pressure": SchoolVerdict(
                    school="sentiment_pressure",
                    bias=Bias.LONG,
                    conviction=0.5,
                    aligned_with_entry=True,
                    rationale="risk-on",
                ),
            },
            composite_bias=Bias.LONG,
            conviction=0.5,
            schools_consulted=1,
            schools_aligned_with_entry=1,
            schools_disagreeing_with_entry=0,
            schools_neutral=0,
            rationale="ok",
        )

    monkeypatch.setattr(sage_pkg, "consult_sage", fake_consult)

    admin = MagicMock()
    intel = JarvisIntelligence(admin=admin, memory=None)
    full = JarvisFull(intelligence=intel)
    bars = [
        {
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "volume": 1000.0 + i,
        }
        for i in range(40)
    ]
    req = _stub_request(
        payload={
            "symbol": "BTCUSDT",
            "side": "long",
            "entry_price": 140.5,
            "sage_bars": bars,
            "funding_basis": {"funding_rate_bps": 2.5},
            "options_greeks": {"iv_25d_skew": 0.04},
            "onchain": {"sopr": 1.02},
            "sentiment": {
                "asset_summaries": [
                    {
                        "asset": "BTC",
                        "fear_greed": 0.75,
                        "social_volume_z": 1.4,
                        "active_topics": ["fomo"],
                    }
                ],
                "pressure": {"status": "risk_on", "score": 0.3},
            },
            "liquidation": {"levels": [{"price": 135.0, "total_size_usd": 25000.0}]},
        },
    )

    report = full._consult_sage_for_request(req)

    assert report is not None
    ctx = captured["ctx"]
    assert ctx.instrument_class == "crypto"
    assert ctx.price == bars[-1]["close"]
    assert ctx.funding == {"funding_rate_bps": 2.5}
    assert ctx.options == {"iv_25d_skew": 0.04}
    assert ctx.onchain == {"sopr": 1.02}
    assert ctx.sentiment["pressure"]["status"] == "risk_on"
    assert ctx.liquidation == {"levels": [{"price": 135.0, "total_size_usd": 25000.0}]}


def _identity_runtime_overrides(monkeypatch) -> None:
    monkeypatch.setattr(
        "eta_engine.brain.jarvis_v3.risk_budget_allocator.current_envelope",
        lambda bot_id=None: SimpleNamespace(multiplier=1.0, reason=""),
    )
    monkeypatch.setattr(
        "eta_engine.brain.jarvis_v3.jarvis_conductor.build_school_inputs_from_sage",
        lambda report: {},
    )
    monkeypatch.setattr(
        "eta_engine.brain.jarvis_v3.jarvis_conductor.orchestrate",
        lambda req, base_size, school_inputs: SimpleNamespace(final_size=base_size, block_reason=None),
    )
    monkeypatch.setattr(
        "eta_engine.brain.jarvis_v3.llm_narrative.llm_narrative",
        lambda consolidated, verbosity="terse", force_template=False: f"{verbosity}-narrative",
    )


def _fake_consolidated(final_size_multiplier: float = 1.0):
    consolidated = MagicMock()
    consolidated.final_size_multiplier = final_size_multiplier
    consolidated.intelligence_enabled = False
    consolidated.operator_override_level = ""
    consolidated.base_reason = ""
    consolidated.final_verdict = "PROCEED"
    consolidated.confidence = 0.0
    consolidated.is_blocked.return_value = False
    return consolidated


def test_jarvis_full_sentiment_headwind_tightens_size(monkeypatch) -> None:
    from eta_engine.brain.jarvis_v3.jarvis_full import JarvisFull
    from eta_engine.brain.jarvis_v3.sage.base import Bias, SageReport, SchoolVerdict

    _identity_runtime_overrides(monkeypatch)
    intelligence = MagicMock()
    intelligence.consult.return_value = _fake_consolidated(1.0)
    full = JarvisFull(intelligence=intelligence)
    full._consult_sage_for_request = MagicMock(
        return_value=SageReport(
            per_school={
                "sentiment_pressure": SchoolVerdict(
                    school="sentiment_pressure",
                    bias=Bias.SHORT,
                    conviction=0.35,
                    aligned_with_entry=False,
                    rationale="macro risk-off",
                    signals={
                        "status": "risk_off",
                        "score": -0.32,
                        "lead_negative_asset": "macro",
                        "selected_assets": ["macro"],
                    },
                ),
            },
            composite_bias=Bias.NEUTRAL,
            conviction=0.2,
            schools_consulted=1,
            schools_aligned_with_entry=0,
            schools_disagreeing_with_entry=1,
            schools_neutral=0,
            rationale="macro risk-off",
        ),
    )

    verdict = full.consult(_stub_request(payload={"side": "long"}))

    assert verdict.final_size_multiplier == 0.75
    assert verdict.sentiment_pressure_status == "risk_off"
    assert verdict.sentiment_modulation == "headwind_strong"
    consulted_req = intelligence.consult.call_args.args[0]
    assert consulted_req.payload["sentiment_pressure_status"] == "risk_off"
    assert consulted_req.payload["sentiment_modulation"] == "headwind_strong"


def test_jarvis_full_sentiment_tailwind_loosens_size(monkeypatch) -> None:
    from eta_engine.brain.jarvis_v3.jarvis_full import JarvisFull
    from eta_engine.brain.jarvis_v3.sage.base import Bias, SageReport, SchoolVerdict

    _identity_runtime_overrides(monkeypatch)
    intelligence = MagicMock()
    intelligence.consult.return_value = _fake_consolidated(1.0)
    full = JarvisFull(intelligence=intelligence)
    full._consult_sage_for_request = MagicMock(
        return_value=SageReport(
            per_school={
                "sentiment_pressure": SchoolVerdict(
                    school="sentiment_pressure",
                    bias=Bias.LONG,
                    conviction=0.62,
                    aligned_with_entry=True,
                    rationale="btc risk-on",
                    signals={
                        "status": "risk_on",
                        "score": 0.28,
                        "lead_positive_asset": "BTC",
                        "selected_assets": ["BTC", "macro"],
                    },
                ),
            },
            composite_bias=Bias.NEUTRAL,
            conviction=0.2,
            schools_consulted=1,
            schools_aligned_with_entry=1,
            schools_disagreeing_with_entry=0,
            schools_neutral=0,
            rationale="btc risk-on",
        ),
    )

    verdict = full.consult(_stub_request(payload={"side": "long"}))

    assert verdict.final_size_multiplier == 1.1
    assert verdict.sentiment_pressure_status == "risk_on"
    assert verdict.sentiment_pressure_lead_asset == "BTC"
    assert verdict.sentiment_modulation == "tailwind"


def test_jarvis_full_persists_bot_and_sentiment_metadata_to_canonical_log(tmp_path: Path, monkeypatch) -> None:
    import json

    from eta_engine.brain.jarvis_v3.intelligence import (
        IntelligenceConfig,
        JarvisIntelligence,
    )
    from eta_engine.brain.jarvis_v3.jarvis_full import JarvisFull
    from eta_engine.brain.jarvis_v3.sage.base import Bias, SageReport, SchoolVerdict

    _identity_runtime_overrides(monkeypatch)
    admin = MagicMock()
    admin.request_approval.return_value = _stub_response("APPROVED")
    verdict_log = tmp_path / "verdicts.jsonl"
    intelligence = JarvisIntelligence(
        admin=admin,
        memory=None,
        cfg=IntelligenceConfig(enable_intelligence=False),
        verdict_log=verdict_log,
    )
    full = JarvisFull(intelligence=intelligence)
    full._consult_sage_for_request = MagicMock(
        return_value=SageReport(
            per_school={
                "sentiment_pressure": SchoolVerdict(
                    school="sentiment_pressure",
                    bias=Bias.LONG,
                    conviction=0.62,
                    aligned_with_entry=True,
                    rationale="btc risk-on",
                    signals={
                        "status": "risk_on",
                        "score": 0.28,
                        "lead_positive_asset": "BTC",
                        "selected_assets": ["BTC", "macro"],
                    },
                ),
            },
            composite_bias=Bias.NEUTRAL,
            conviction=0.2,
            schools_consulted=1,
            schools_aligned_with_entry=1,
            schools_disagreeing_with_entry=0,
            schools_neutral=0,
            rationale="btc risk-on",
        ),
    )

    verdict = full.consult(_stub_request(payload={"side": "long"}), bot_id="btc_hybrid")

    persisted = json.loads(verdict_log.read_text(encoding="utf-8").splitlines()[-1])
    assert verdict.consolidated.bot_id == "btc_hybrid"
    assert verdict.consolidated.sentiment_pressure_status == "risk_on"
    assert verdict.consolidated.sentiment_modulation == "tailwind"
    assert persisted["bot_id"] == "btc_hybrid"
    assert persisted["sentiment_pressure_status"] == "risk_on"
    assert persisted["sentiment_pressure_score"] == 0.28
    assert persisted["sentiment_pressure_lead_asset"] == "BTC"
    assert persisted["sentiment_modulation"] == "tailwind"
