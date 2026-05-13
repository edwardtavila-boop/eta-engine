"""Tests for jarvis_conductor.orchestrate — the JARVIS Supercharge entrypoint.

The conductor is the *only* new code that hooks into JarvisFull.consult().
These tests verify the 5-stream pipeline composes correctly and that any
single-stream failure falls back to legacy behavior (never raises).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from eta_engine.brain.jarvis_v3 import jarvis_conductor as jc
from eta_engine.brain.jarvis_v3 import portfolio_brain
from eta_engine.brain.jarvis_v3 import trace_emitter as te


@dataclass
class _FakeReq:
    bot_id: str = "test_bot"
    asset_class: str = "BTC"
    symbol: str = "BTC"
    action: str = "ENTER"


def _healthy_ctx() -> portfolio_brain.PortfolioContext:
    return portfolio_brain.PortfolioContext(
        fleet_long_notional_by_asset={},
        fleet_short_notional_by_asset={},
        recent_entries_by_asset={},
        open_correlated_exposure=0.0,
        portfolio_drawdown_today_r=0.0,
        fleet_kill_active=False,
    )


def _kill_ctx() -> portfolio_brain.PortfolioContext:
    return portfolio_brain.PortfolioContext(
        fleet_long_notional_by_asset={},
        fleet_short_notional_by_asset={},
        recent_entries_by_asset={},
        open_correlated_exposure=0.0,
        portfolio_drawdown_today_r=0.0,
        fleet_kill_active=True,
    )


def test_orchestrate_healthy_returns_base_size(monkeypatch, tmp_path):
    """Healthy state: orchestrate returns base_size unchanged and a consult_id."""
    monkeypatch.setattr(portfolio_brain, "snapshot", _healthy_ctx)
    trace_path = tmp_path / "trace.jsonl"
    result = jc.orchestrate(req=_FakeReq(), base_size=1.0, trace_path=trace_path)

    assert result.final_size == pytest.approx(1.0, abs=0.01)
    assert result.consult_id != ""
    assert result.block_reason is None
    assert result.portfolio_modifier == pytest.approx(1.0, abs=0.01)


def test_orchestrate_blocks_when_fleet_kill(monkeypatch, tmp_path):
    """Fleet kill → final_size 0.0 and block_reason set."""
    monkeypatch.setattr(portfolio_brain, "snapshot", _kill_ctx)
    trace_path = tmp_path / "trace.jsonl"
    result = jc.orchestrate(req=_FakeReq(), base_size=1.0, trace_path=trace_path)

    assert result.final_size == 0.0
    assert result.block_reason == "fleet_kill_active"


def test_orchestrate_writes_exactly_one_trace_line(monkeypatch, tmp_path):
    """Every consult emits one JSON line."""
    monkeypatch.setattr(portfolio_brain, "snapshot", _healthy_ctx)
    trace_path = tmp_path / "trace.jsonl"
    jc.orchestrate(req=_FakeReq(), base_size=1.0, trace_path=trace_path)

    assert trace_path.exists()
    contents = trace_path.read_text(encoding="utf-8")
    assert contents.count("\n") == 1


def test_orchestrate_never_raises_when_portfolio_brain_fails(monkeypatch, tmp_path):
    """Any single-stream failure → legacy fallback, no exception."""

    def boom():
        raise RuntimeError("portfolio brain exploded")

    monkeypatch.setattr(portfolio_brain, "snapshot", boom)
    trace_path = tmp_path / "trace.jsonl"
    # MUST NOT raise
    result = jc.orchestrate(req=_FakeReq(), base_size=1.2, trace_path=trace_path)
    assert result is not None
    # Legacy fallback: base_size passes through (clamped to 1.5)
    assert result.final_size == pytest.approx(1.2, abs=0.01)
    assert result.block_reason is None  # no veto when brain failed


def test_orchestrate_never_raises_when_hot_learner_fails(monkeypatch, tmp_path):
    """hot_learner failure → empty weights, consult still completes."""
    from eta_engine.brain.jarvis_v3 import hot_learner

    def boom(asset):
        raise RuntimeError("hot learner exploded")

    monkeypatch.setattr(portfolio_brain, "snapshot", _healthy_ctx)
    monkeypatch.setattr(hot_learner, "current_weights", boom)
    trace_path = tmp_path / "trace.jsonl"

    result = jc.orchestrate(req=_FakeReq(), base_size=1.0, trace_path=trace_path)
    assert result.school_weights == {}
    assert result.final_size > 0.0


def test_orchestrate_never_raises_when_trace_emitter_fails(monkeypatch, tmp_path):
    """trace_emitter failure → consult still returns a valid result."""

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(portfolio_brain, "snapshot", _healthy_ctx)
    monkeypatch.setattr(te, "emit", boom)

    # Must not raise
    result = jc.orchestrate(req=_FakeReq(), base_size=1.0, trace_path=tmp_path / "x.jsonl")
    assert result is not None
    assert result.final_size > 0.0


def test_orchestrate_passes_hot_learner_weights(monkeypatch, tmp_path):
    """Hot learner weights surface in the result for downstream Sage weighting."""
    from eta_engine.brain.jarvis_v3 import hot_learner

    monkeypatch.setattr(portfolio_brain, "snapshot", _healthy_ctx)
    monkeypatch.setattr(
        hot_learner,
        "current_weights",
        lambda asset: {"order_flow": 1.3, "wyckoff": 0.7},
    )
    trace_path = tmp_path / "trace.jsonl"

    result = jc.orchestrate(req=_FakeReq(), base_size=1.0, trace_path=trace_path)
    assert result.school_weights == {"order_flow": 1.3, "wyckoff": 0.7}


def test_orchestrate_clamps_size_to_max_1_5(monkeypatch, tmp_path):
    """Final size never exceeds 1.5 even when base × modifier would."""

    # Force portfolio_modifier > 1.0 via a custom snapshot+assess pair.
    def fake_snapshot():
        # any healthy context — assess() returns 1.0 in default rules,
        # so we test the conductor's own clamp by passing a large base_size.
        return _healthy_ctx()

    monkeypatch.setattr(portfolio_brain, "snapshot", fake_snapshot)
    trace_path = tmp_path / "trace.jsonl"
    result = jc.orchestrate(req=_FakeReq(), base_size=2.5, trace_path=trace_path)
    assert result.final_size <= 1.5 + 1e-9


def test_observe_close_forwards_to_hot_learner(monkeypatch):
    """observe_close passes args through to hot_learner.observe_close."""
    from eta_engine.brain.jarvis_v3 import hot_learner

    captured: dict = {}

    def fake_obs(asset, school_attribution, r_outcome):
        captured["asset"] = asset
        captured["attribution"] = school_attribution
        captured["r"] = r_outcome

    monkeypatch.setattr(hot_learner, "observe_close", fake_obs)
    jc.observe_close(
        asset_class="BTC",
        school_attribution={"order_flow": 0.5},
        r_outcome=1.2,
    )
    assert captured["asset"] == "BTC"
    assert captured["attribution"] == {"order_flow": 0.5}
    assert captured["r"] == 1.2


def test_observe_close_never_raises(monkeypatch):
    """observe_close swallows hot_learner failures."""
    from eta_engine.brain.jarvis_v3 import hot_learner

    def boom(**kwargs):
        raise RuntimeError("learner is on fire")

    monkeypatch.setattr(hot_learner, "observe_close", boom)
    # MUST NOT raise
    jc.observe_close(
        asset_class="BTC",
        school_attribution={"a": 1.0},
        r_outcome=1.0,
    )


def test_consult_id_unique_across_calls(monkeypatch, tmp_path):
    """Every orchestrate call gets a fresh consult_id."""
    monkeypatch.setattr(portfolio_brain, "snapshot", _healthy_ctx)
    trace_path = tmp_path / "trace.jsonl"

    ids = set()
    for _ in range(10):
        r = jc.orchestrate(req=_FakeReq(), base_size=1.0, trace_path=trace_path)
        ids.add(r.consult_id)
    assert len(ids) == 10


# ---------------------------------------------------------------------------
# build_school_inputs_from_sage — schema v2 per-school RAW vote translator
# ---------------------------------------------------------------------------


def test_build_school_inputs_returns_empty_for_none() -> None:
    """No sage_report → empty dict (back-compat for consults where sage didn't fire)."""
    assert jc.build_school_inputs_from_sage(None) == {}


def test_build_school_inputs_returns_empty_for_malformed_report() -> None:
    """Object without per_school attribute → empty dict, no exception."""

    class Garbage:
        pass

    assert jc.build_school_inputs_from_sage(Garbage()) == {}


def test_build_school_inputs_signs_score_by_alignment() -> None:
    """Aligned=+conviction, misaligned=-conviction, neutral=0."""
    from eta_engine.brain.jarvis_v3.sage import base as sage_base

    aligned_long = sage_base.SchoolVerdict(
        school="momentum",
        bias=sage_base.Bias.LONG,
        conviction=0.8,
        aligned_with_entry=True,
        rationale="trend up",
    )
    misaligned_short = sage_base.SchoolVerdict(
        school="mean_revert",
        bias=sage_base.Bias.SHORT,
        conviction=0.6,
        aligned_with_entry=False,
        rationale="overbought",
    )
    neutral = sage_base.SchoolVerdict(
        school="risk",
        bias=sage_base.Bias.NEUTRAL,
        conviction=0.0,
        aligned_with_entry=False,
    )

    report = sage_base.SageReport(
        per_school={
            "momentum": aligned_long,
            "mean_revert": misaligned_short,
            "risk": neutral,
        },
        composite_bias=sage_base.Bias.LONG,
        conviction=0.5,
        schools_consulted=3,
        schools_aligned_with_entry=1,
        schools_disagreeing_with_entry=1,
        schools_neutral=1,
    )
    result = jc.build_school_inputs_from_sage(report)
    assert result["momentum"]["score"] == 0.8
    assert result["mean_revert"]["score"] == -0.6
    assert result["risk"]["score"] == 0.0
    assert result["momentum"]["bias"] == "long"
    assert result["mean_revert"]["bias"] == "short"
    assert result["risk"]["bias"] == "neutral"


def test_build_school_inputs_preserves_rationale_truncated() -> None:
    """Rationale is preserved (truncated to 200 chars) for operator inspection."""
    from eta_engine.brain.jarvis_v3.sage import base as sage_base

    long_text = "x" * 500
    verdict = sage_base.SchoolVerdict(
        school="s",
        bias=sage_base.Bias.LONG,
        conviction=0.5,
        aligned_with_entry=True,
        rationale=long_text,
    )
    report = sage_base.SageReport(
        per_school={"s": verdict},
        composite_bias=sage_base.Bias.LONG,
        conviction=0.5,
        schools_consulted=1,
        schools_aligned_with_entry=1,
        schools_disagreeing_with_entry=0,
        schools_neutral=0,
    )
    out = jc.build_school_inputs_from_sage(report)
    assert len(out["s"]["rationale"]) <= 200


def test_build_school_inputs_never_raises_on_hostile_verdict() -> None:
    """A SchoolVerdict-like object whose attributes raise → that school is
    skipped silently; other schools still produce inputs."""
    from eta_engine.brain.jarvis_v3.sage import base as sage_base

    class HostileVerdict:
        @property
        def bias(self):
            raise RuntimeError("won't expose bias")

    good = sage_base.SchoolVerdict(
        school="good",
        bias=sage_base.Bias.LONG,
        conviction=0.7,
        aligned_with_entry=True,
    )

    class FakeReport:
        per_school = {"hostile": HostileVerdict(), "good": good}

    out = jc.build_school_inputs_from_sage(FakeReport())
    # Only the good school survived
    assert "good" in out
    assert "hostile" not in out


def test_orchestrate_with_school_inputs_emits_them_to_trace(monkeypatch, tmp_path) -> None:
    """When orchestrate receives school_inputs, the emitted v2 record includes them."""
    monkeypatch.setattr(portfolio_brain, "snapshot", _healthy_ctx)
    trace_path = tmp_path / "trace.jsonl"

    school_inputs = {
        "momentum": {"score": 0.7, "conviction": 0.7, "bias": "long", "rationale": "trend up", "rng_seed": None},
        "mean_revert": {"score": -0.3, "conviction": 0.3, "bias": "short", "rationale": "overbought", "rng_seed": None},
    }
    jc.orchestrate(
        req=_FakeReq(),
        base_size=1.0,
        trace_path=trace_path,
        school_inputs=school_inputs,
    )
    records = te.tail(n=1, path=trace_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["schema_version"] == 2
    assert rec["school_inputs"]["momentum"]["score"] == 0.7
    assert rec["school_inputs"]["mean_revert"]["score"] == -0.3


def test_orchestrate_without_school_inputs_emits_empty(monkeypatch, tmp_path) -> None:
    """Backward-compat: calling orchestrate without school_inputs still works."""
    monkeypatch.setattr(portfolio_brain, "snapshot", _healthy_ctx)
    trace_path = tmp_path / "trace.jsonl"

    jc.orchestrate(req=_FakeReq(), base_size=1.0, trace_path=trace_path)
    records = te.tail(n=1, path=trace_path)
    # School_inputs is empty but the record is still v2
    assert records[0]["schema_version"] == 2
    assert records[0]["school_inputs"] == {}
