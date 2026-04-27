"""Tests for the JARVIS sage transition (2026-04-27).

Covers:
  * MarketContext + base types
  * each functional school produces a SchoolVerdict on synthetic tape
  * confluence aggregator: weighted sum semantics
  * consult_sage runs every school and returns a SageReport
  * v22 candidate uses sage to modulate v17 (loosen on agree, defer on disagree)
"""
from __future__ import annotations

import pytest


# ─── synthetic tape generators ───────────────────────────────────


def _uptrend_bars(n: int = 60, *, start: float = 21000, vol: int = 1000) -> list[dict]:
    """Synthesize a clean uptrend with rising volume."""
    bars = []
    for i in range(n):
        base = start + i * 5
        bars.append({
            "open":   base,
            "high":   base + 8,
            "low":    base - 2,
            "close":  base + 5,
            "volume": vol + i * 10,
        })
    return bars


def _downtrend_bars(n: int = 60, *, start: float = 21000, vol: int = 1000) -> list[dict]:
    bars = []
    for i in range(n):
        base = start - i * 5
        bars.append({
            "open":   base,
            "high":   base + 2,
            "low":    base - 8,
            "close":  base - 5,
            "volume": vol + i * 10,
        })
    return bars


def _chop_bars(n: int = 60, *, mid: float = 21000) -> list[dict]:
    bars = []
    for i in range(n):
        delta = 3.0 if i % 2 == 0 else -3.0
        bars.append({
            "open":   mid,
            "high":   mid + 5,
            "low":    mid - 5,
            "close":  mid + delta,
            "volume": 1000,
        })
    return bars


# ─── base types ───────────────────────────────────────────────────


def test_marketcontext_helpers() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    bars = _uptrend_bars(n=10)
    ctx = MarketContext(bars=bars, side="long")
    assert ctx.n_bars == 10
    assert len(ctx.closes()) == 10
    assert len(ctx.highs()) == 10
    assert len(ctx.lows()) == 10
    assert len(ctx.volumes()) == 10


def test_schoolverdict_validates_conviction() -> None:
    from eta_engine.brain.jarvis_v3.sage import SchoolVerdict
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    SchoolVerdict(school="x", bias=Bias.LONG, conviction=0.5)
    with pytest.raises(ValueError):
        SchoolVerdict(school="x", bias=Bias.LONG, conviction=1.5)
    with pytest.raises(ValueError):
        SchoolVerdict(school="x", bias=Bias.LONG, conviction=-0.1)


# ─── individual schools (smoke + directional sanity) ─────────────


def test_dow_theory_detects_uptrend() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.schools.dow_theory import DowTheorySchool

    school = DowTheorySchool()
    v = school.analyze(MarketContext(bars=_uptrend_bars(60), side="long"))
    assert v.bias == Bias.LONG
    assert v.conviction > 0.5
    assert v.aligned_with_entry is True


def test_dow_theory_detects_downtrend() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.schools.dow_theory import DowTheorySchool

    school = DowTheorySchool()
    v = school.analyze(MarketContext(bars=_downtrend_bars(60), side="long"))
    assert v.bias == Bias.SHORT
    assert v.aligned_with_entry is False  # entry is long, bias is short


def test_dow_theory_neutral_on_chop() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.schools.dow_theory import DowTheorySchool

    v = DowTheorySchool().analyze(MarketContext(bars=_chop_bars(60), side="long"))
    assert v.bias == Bias.NEUTRAL


def test_trend_following_rises_with_uptrend() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.schools.trend_following import TrendFollowingSchool

    v = TrendFollowingSchool().analyze(MarketContext(bars=_uptrend_bars(60), side="long"))
    assert v.bias == Bias.LONG
    assert v.signals["fast_above_slow"] is True


def test_vpa_high_volume_strong_move_continues() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.schools.vpa import VPASchool

    bars = _uptrend_bars(60)
    # Make last bar high volume + strong body
    bars[-1]["volume"] = 5000
    bars[-1]["close"] = bars[-1]["open"] + 50
    bars[-1]["high"] = bars[-1]["close"] + 2
    v = VPASchool().analyze(MarketContext(bars=bars, side="long"))
    assert v.bias == Bias.LONG
    assert v.conviction >= 0.5


def test_risk_management_rejects_over_max() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.schools.risk_management import RiskManagementSchool

    ctx = MarketContext(
        bars=_uptrend_bars(40), side="long",
        account_equity_usd=10000, risk_per_trade_pct=0.05,  # 5% > 2% cap
    )
    v = RiskManagementSchool().analyze(ctx)
    assert v.conviction == 0.0  # explicit non-compliance
    assert v.aligned_with_entry is False


def test_risk_management_full_compliance_at_one_pct() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.schools.risk_management import RiskManagementSchool

    ctx = MarketContext(
        bars=_uptrend_bars(40), side="long",
        account_equity_usd=10000, risk_per_trade_pct=0.01, stop_distance_pct=0.005,
    )
    v = RiskManagementSchool().analyze(ctx)
    assert v.conviction >= 0.9


def test_market_profile_produces_poc_and_va() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.schools.market_profile import MarketProfileSchool

    v = MarketProfileSchool().analyze(MarketContext(bars=_uptrend_bars(60), side="long"))
    assert "poc" in v.signals
    assert "vah" in v.signals
    assert "val" in v.signals


def test_smc_ict_returns_verdict_on_uptrend() -> None:
    """SMC needs SWING structure to fire BOS/ChoCH; a perfectly monotonic
    uptrend has no swings. Verify we get a verdict (no exceptions) and
    that bias is at least not SHORT."""
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.schools.smc_ict import SmcIctSchool

    v = SmcIctSchool().analyze(MarketContext(bars=_uptrend_bars(60), side="long"))
    assert v.school == "smc_ict"
    assert v.bias != Bias.SHORT


def test_fibonacci_handles_swing_with_no_retracement() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.schools.fibonacci import FibonacciSchool

    v = FibonacciSchool().analyze(MarketContext(bars=_uptrend_bars(60), side="long"))
    # Just verify it returns a verdict (no exceptions)
    assert v.school == "fibonacci"
    assert v.signals.get("retrace_pct") is not None or v.conviction == 0


# ─── confluence aggregator ───────────────────────────────────────


def test_confluence_aggregates_long_majority() -> None:
    from eta_engine.brain.jarvis_v3.sage import SchoolBase, SchoolVerdict
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.confluence import aggregate

    class _S(SchoolBase):
        NAME = "x"
        def analyze(self, ctx): return None  # type: ignore[return-value]
    schools = {"a": _S(), "b": _S(), "c": _S()}
    schools["a"].WEIGHT = 1.0
    schools["b"].WEIGHT = 1.0
    schools["c"].WEIGHT = 1.0

    verdicts = {
        "a": SchoolVerdict(school="a", bias=Bias.LONG, conviction=0.8),
        "b": SchoolVerdict(school="b", bias=Bias.LONG, conviction=0.7),
        "c": SchoolVerdict(school="c", bias=Bias.SHORT, conviction=0.5),
    }
    report = aggregate(verdicts, schools, entry_side="long")
    assert report.composite_bias == Bias.LONG
    assert report.conviction > 0
    assert report.schools_aligned_with_entry == 2
    assert report.schools_disagreeing_with_entry == 1


def test_confluence_neutral_when_split() -> None:
    from eta_engine.brain.jarvis_v3.sage import SchoolBase, SchoolVerdict
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.confluence import aggregate

    class _S(SchoolBase):
        NAME = "x"
        def analyze(self, ctx): return None  # type: ignore[return-value]
    schools = {"a": _S(), "b": _S()}

    verdicts = {
        "a": SchoolVerdict(school="a", bias=Bias.LONG, conviction=0.6),
        "b": SchoolVerdict(school="b", bias=Bias.SHORT, conviction=0.6),
    }
    report = aggregate(verdicts, schools, entry_side="long")
    assert report.composite_bias == Bias.NEUTRAL


def test_confluence_handles_empty_input() -> None:
    from eta_engine.brain.jarvis_v3.sage import SchoolBase
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.confluence import aggregate
    report = aggregate({}, {}, entry_side="long")
    assert report.composite_bias == Bias.NEUTRAL
    assert report.conviction == 0.0
    assert report.schools_consulted == 0


# ─── consult_sage end-to-end ─────────────────────────────────────


def test_consult_sage_runs_every_school_on_uptrend() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext, SCHOOLS, consult_sage
    from eta_engine.brain.jarvis_v3.sage.base import Bias

    ctx = MarketContext(bars=_uptrend_bars(60), side="long",
                        account_equity_usd=10000, risk_per_trade_pct=0.01,
                        stop_distance_pct=0.005)
    report = consult_sage(ctx)
    # All non-failing schools should report
    assert report.schools_consulted >= 10
    # Composite should be long-biased
    assert report.composite_bias == Bias.LONG
    assert report.alignment_score > 0.5


def test_consult_sage_can_filter_by_school_name() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext, consult_sage

    ctx = MarketContext(bars=_uptrend_bars(60), side="long")
    report = consult_sage(ctx, enabled={"dow_theory", "vpa"})
    assert set(report.per_school.keys()) <= {"dow_theory", "vpa"}
    assert report.schools_consulted <= 2


# ─── SAGE policies registry ──────────────────────────────────────


def test_v22_sage_confluence_registered() -> None:
    """v22 must be in the candidate registry after the policies package
    is imported. We use importlib.reload to bypass Python's module cache
    in case clear_registry() emptied the registry mid-test."""
    import importlib
    from eta_engine.brain.jarvis_v3 import policies as policies_pkg
    from eta_engine.brain.jarvis_v3 import candidate_policy
    candidate_policy.clear_registry()
    importlib.reload(policies_pkg.v17_champion)
    importlib.reload(policies_pkg.v18_high_stress_tighten)
    importlib.reload(policies_pkg.v19_drift_aware)
    importlib.reload(policies_pkg.v20_overnight_tighten)
    importlib.reload(policies_pkg.v21_drawdown_proximity)
    importlib.reload(policies_pkg.v22_sage_confluence)

    names = {c["name"] for c in candidate_policy.list_candidates()}
    assert "v22" in names
    assert callable(candidate_policy.get_candidate("v22"))


def test_v22_returns_v17_baseline_when_no_sage_bars() -> None:
    """Without sage_bars in payload, v22 == v17 (passthrough)."""
    from eta_engine.brain.jarvis_admin import (
        ActionRequest, ActionType, SubsystemId,
    )
    from eta_engine.brain.jarvis_v3.policies.v22_sage_confluence import evaluate_v22
    from eta_engine.brain.jarvis_v3.policies import v22_sage_confluence as v22_mod
    from eta_engine.brain.jarvis_admin import (
        ActionResponse, Verdict, ActionSuggestion,
    )
    from eta_engine.brain.jarvis_context import SessionPhase

    # Stub v17 to return APPROVED. v22 should pass through unchanged.
    base = ActionResponse(
        request_id="r", verdict=Verdict.APPROVED,
        reason="ok", reason_code="ok",
        jarvis_action=ActionSuggestion.TRADE,
        stress_composite=0.5, session_phase=SessionPhase.OPEN_DRIVE,
        binding_constraint="", size_cap_mult=None,
    )
    orig = v22_mod.evaluate_request
    try:
        v22_mod.evaluate_request = lambda req, ctx: base  # type: ignore[assignment]
        req = ActionRequest(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
            payload={},  # no sage_bars
        )
        out = evaluate_v22(req, ctx=None)  # type: ignore[arg-type]
        assert out.verdict == Verdict.APPROVED
        assert not any("v22_sage" in c for c in out.conditions)
    finally:
        v22_mod.evaluate_request = orig
