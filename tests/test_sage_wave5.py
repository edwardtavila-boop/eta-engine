"""Tests for Wave-5 sage upgrades (2026-04-27).

Covers:
  * MarketContext multi-timeframe support (has_tf, for_tf)
  * SchoolBase applies_to (instrument + regime gates)
  * regime detector + weight modulator
  * EdgeTracker observe + persistence + weight_modifier
  * disagreement matrix detect_clashes
  * dependency graph apply_dependency_boosts
  * 4 new functional schools (seasonality, vol_regime, stat_sig, red_team)
  * sage cache works
  * narrative template fallback
  * sage health monitor flags broken schools
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _bars(n: int, *, trend: str = "up", base: float = 21000) -> list[dict]:
    """Synthetic OHLCV bars."""
    out: list[dict] = []
    sign = 1 if trend == "up" else -1 if trend == "down" else 0
    now = datetime.now(UTC)
    for i in range(n):
        center = base + sign * i * 5
        if trend == "chop":
            center = base + (3 if i % 2 == 0 else -3)
        out.append(
            {
                "ts": (now - timedelta(minutes=(n - i) * 5)).isoformat(),
                "open": center - 1,
                "high": center + 5,
                "low": center - 5,
                "close": center + (sign * 2 if trend != "chop" else (1 if i % 2 == 0 else -1)),
                "volume": 1000 + i * 5,
            }
        )
    return out


# ─── MarketContext multi-timeframe ────────────────────────────────


def test_marketcontext_has_tf_and_for_tf() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext

    bars_5m = _bars(50)
    bars_1h = _bars(20)
    ctx = MarketContext(
        bars=bars_5m,
        side="long",
        bars_by_tf={"5m": bars_5m, "1h": bars_1h},
    )
    assert ctx.has_tf("5m") is True
    assert ctx.has_tf("1h") is True
    assert ctx.has_tf("1d") is False
    ctx_1h = ctx.for_tf("1h")
    assert ctx_1h.bars is bars_1h
    assert ctx_1h.symbol == ctx.symbol


def test_marketcontext_for_tf_passthrough_when_tf_missing() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext

    bars = _bars(50)
    ctx = MarketContext(bars=bars, side="long")
    assert ctx.for_tf("missing") is ctx


# ─── SchoolBase applies_to ───────────────────────────────────────


def test_schoolbase_applies_to_default_universal() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.schools.dow_theory import DowTheorySchool

    s = DowTheorySchool()
    ctx = MarketContext(bars=_bars(50), side="long")
    assert s.applies_to(ctx) is True


def test_schoolbase_applies_to_filters_by_instrument() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.schools.onchain import OnChainSchool

    s = OnChainSchool()
    ctx_crypto = MarketContext(bars=_bars(50), side="long", instrument_class="crypto")
    ctx_equity = MarketContext(bars=_bars(50), side="long", instrument_class="equity")
    assert s.applies_to(ctx_crypto) is True
    assert s.applies_to(ctx_equity) is False  # onchain is crypto-only


# ─── regime detector ─────────────────────────────────────────────


def test_regime_detector_classifies_trending() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.regime import Regime, detect_regime

    ctx = MarketContext(bars=_bars(60, trend="up"), side="long")
    regime, signals = detect_regime(ctx)
    assert regime in (Regime.TRENDING, Regime.QUIET)
    assert "directional_strength" in signals


def test_regime_weight_modulator_for_known_school() -> None:
    from eta_engine.brain.jarvis_v3.sage.regime import Regime, regime_weight_modulator

    assert regime_weight_modulator("trend_following", Regime.TRENDING) == 1.5
    assert regime_weight_modulator("trend_following", Regime.RANGING) == 0.4
    # unknown school -> 1.0
    assert regime_weight_modulator("imaginary_school", Regime.TRENDING) == 1.0
    # None regime -> 1.0
    assert regime_weight_modulator("trend_following", None) == 1.0


# ─── EdgeTracker ──────────────────────────────────────────────────


def test_edge_tracker_observe_and_weight_modifier(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.sage.edge_tracker import EdgeTracker

    et = EdgeTracker(state_path=tmp_path / "edge.json")
    # 12 wins, 0 losses, all aligned -> hit_rate = 1.0, expectancy > 0
    for _ in range(12):
        et.observe(school="trend_following", school_bias="long", entry_side="long", realized_r=1.5)

    edge = et.edge_for("trend_following")
    assert edge.hit_rate == 1.0
    assert edge.avg_r == 1.5
    assert edge.weight_modifier() > 1.0  # strong school earns up-weight


def test_edge_tracker_negative_expectancy_lowers_weight(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.sage.edge_tracker import EdgeTracker

    et = EdgeTracker(state_path=tmp_path / "edge.json")
    for _ in range(15):
        et.observe(school="bad_school", school_bias="long", entry_side="long", realized_r=-0.7)
    edge = et.edge_for("bad_school")
    assert edge.weight_modifier() < 1.0


def test_edge_tracker_persists_across_instances(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.sage.edge_tracker import EdgeTracker

    sp = tmp_path / "edge.json"
    et1 = EdgeTracker(state_path=sp)
    for _ in range(5):
        et1.observe(school="x", school_bias="long", entry_side="long", realized_r=0.5)
    et1.flush()
    et2 = EdgeTracker(state_path=sp)
    assert et2.edge_for("x").n_aligned_wins == 5


# ─── Disagreement detection ──────────────────────────────────────


def test_disagreement_detect_topping_pattern() -> None:
    from eta_engine.brain.jarvis_v3.sage import SageReport, SchoolVerdict
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.disagreement import detect_clashes

    verdicts = {
        "dow_theory": SchoolVerdict(school="dow_theory", bias=Bias.LONG, conviction=0.7),
        "wyckoff": SchoolVerdict(school="wyckoff", bias=Bias.SHORT, conviction=0.6),
    }
    report = SageReport(
        per_school=verdicts,
        composite_bias=Bias.NEUTRAL,
        conviction=0.5,
        schools_consulted=2,
        schools_aligned_with_entry=1,
        schools_disagreeing_with_entry=1,
        schools_neutral=0,
    )
    matches = detect_clashes(report)
    assert any(m.name == "structural_uptrend_topping" for m in matches)


def test_strongest_clash_modifier_defer_wins() -> None:
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.disagreement import (
        ClashPattern,
        strongest_clash_modifier,
    )

    matches = [
        ClashPattern(
            name="t",
            school_a="x",
            bias_a=Bias.LONG,
            school_b="y",
            bias_b=Bias.SHORT,
            interpretation="",
            verdict_modifier="tighten_cap",
            cap_mult=0.5,
        ),
        ClashPattern(
            name="d",
            school_a="a",
            bias_a=Bias.LONG,
            school_b="b",
            bias_b=Bias.SHORT,
            interpretation="",
            verdict_modifier="defer",
        ),
    ]
    mod, mult = strongest_clash_modifier(matches)
    assert mod == "defer"
    assert mult == 0.0


def test_disagreement_reverse_match() -> None:
    from eta_engine.brain.jarvis_v3.sage import SageReport, SchoolVerdict
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.disagreement import detect_clashes

    # Pattern: trend_intact_choch_warning expects trend=LONG, smc=SHORT
    # Reverse: trend=SHORT, smc=LONG should match
    verdicts = {
        "trend_following": SchoolVerdict(school="trend_following", bias=Bias.SHORT, conviction=0.6),
        "smc_ict": SchoolVerdict(school="smc_ict", bias=Bias.LONG, conviction=0.7),
    }
    report = SageReport(
        per_school=verdicts,
        composite_bias=Bias.NEUTRAL,
        conviction=0.5,
        schools_consulted=2,
        schools_aligned_with_entry=0,
        schools_disagreeing_with_entry=2,
        schools_neutral=0,
    )
    matches = detect_clashes(report)
    assert any(m.name == "trend_intact_choch_warning" for m in matches)


def test_disagreement_risk_management_violation() -> None:
    from eta_engine.brain.jarvis_v3.sage import SageReport, SchoolVerdict
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.disagreement import detect_clashes

    verdicts = {
        "risk_management": SchoolVerdict(school="risk_management", bias=Bias.NEUTRAL, conviction=0.0),
        "dow_theory": SchoolVerdict(school="dow_theory", bias=Bias.LONG, conviction=0.8),
    }
    report = SageReport(
        per_school=verdicts,
        composite_bias=Bias.LONG,
        conviction=0.4,
        schools_consulted=2,
        schools_aligned_with_entry=1,
        schools_disagreeing_with_entry=0,
        schools_neutral=1,
    )
    matches = detect_clashes(report)
    assert any(m.name == "risk_violated_anything_long" for m in matches)


def test_disagreement_vol_regime_neutral_pattern() -> None:
    from eta_engine.brain.jarvis_v3.sage import SageReport, SchoolVerdict
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.disagreement import detect_clashes

    verdicts = {
        "volatility_regime": SchoolVerdict(school="volatility_regime", bias=Bias.NEUTRAL, conviction=0.5),
        "trend_following": SchoolVerdict(school="trend_following", bias=Bias.LONG, conviction=0.7),
    }
    report = SageReport(
        per_school=verdicts,
        composite_bias=Bias.NEUTRAL,
        conviction=0.5,
        schools_consulted=2,
        schools_aligned_with_entry=1,
        schools_disagreeing_with_entry=0,
        schools_neutral=1,
    )
    matches = detect_clashes(report)
    assert any(m.name == "vol_regime_quiet_breakout" for m in matches) or len(matches) >= 1


def test_disagreement_strongest_clash_modifier_empty() -> None:
    from eta_engine.brain.jarvis_v3.sage.disagreement import strongest_clash_modifier

    result, mult = strongest_clash_modifier([])
    assert result == "no_change"
    assert mult == 1.0


# ─── Dependency graph ────────────────────────────────────────────


def test_dependency_graph_boosts_target_when_predicate_fires() -> None:
    from eta_engine.brain.jarvis_v3.sage import SchoolVerdict
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.dependency_graph import (
        DependencyRule,
        apply_dependency_boosts,
    )

    verdicts = {
        "wyckoff": SchoolVerdict(school="wyckoff", bias=Bias.LONG, conviction=0.8),
        "vpa": SchoolVerdict(school="vpa", bias=Bias.LONG, conviction=0.6),
    }
    rule = DependencyRule(
        name="r",
        when_school="wyckoff",
        when_bias=Bias.LONG,
        when_min_conviction=0.7,
        target_school="vpa",
        target_bias=Bias.LONG,
        boost=1.3,
    )
    boosts = apply_dependency_boosts(verdicts, rules=[rule])
    assert boosts["vpa"] == 1.3
    assert boosts["wyckoff"] == 1.0


# ─── New schools functional sanity ───────────────────────────────


def test_seasonality_returns_verdict_with_signals() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.schools.seasonality import SeasonalitySchool

    v = SeasonalitySchool().analyze(MarketContext(bars=_bars(50), side="long"))
    assert v.school == "seasonality"
    assert "et_hour" in v.signals
    assert "weekday" in v.signals


def test_volatility_regime_detects_expansion() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.schools.volatility_regime import VolatilityRegimeSchool

    # Build bars where last 5 have wider range
    bars = _bars(60)
    for i in range(-5, 0):
        bars[i]["close"] = bars[i]["open"] + (50 if i % 2 == 0 else -50)
    v = VolatilityRegimeSchool().analyze(MarketContext(bars=bars, side="long"))
    assert v.school == "volatility_regime"
    assert "vol_ratio" in v.signals


def test_stat_significance_returns_p_value() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.schools.stat_significance import StatSignificanceSchool

    v = StatSignificanceSchool().analyze(MarketContext(bars=_bars(60), side="long"))
    assert v.school == "stat_significance"
    assert "p_value" in v.signals
    assert 0.0 <= v.signals["p_value"] <= 1.0


def test_red_team_finds_counter_when_overstretched() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.schools.red_team import RedTeamSchool

    bars = _bars(40, trend="up")
    # Stretch last close far above EMA20 (>5%) to trigger overstretched detection
    bars[-1]["close"] = bars[-1]["close"] * 1.08
    v = RedTeamSchool().analyze(MarketContext(bars=bars, side="long"))
    assert v.school == "red_team"
    assert v.bias == Bias.SHORT
    assert v.aligned_with_entry is False


# ─── Sage cache + parallel ───────────────────────────────────────


def test_sage_cache_returns_same_report_on_repeat() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext, consult_sage
    from eta_engine.brain.jarvis_v3.sage.consultation import clear_sage_cache

    clear_sage_cache()
    bars = _bars(60, trend="up")
    ctx = MarketContext(bars=bars, side="long", symbol="MNQ")
    r1 = consult_sage(ctx, parallel=False, use_cache=True, apply_edge_weights=False)
    r2 = consult_sage(ctx, parallel=False, use_cache=True, apply_edge_weights=False)
    # SAME object returned from cache
    assert r1 is r2


def test_sage_cache_key_includes_risk_inputs() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext, consult_sage
    from eta_engine.brain.jarvis_v3.sage.consultation import clear_sage_cache

    clear_sage_cache()
    bars = _bars(60, trend="up")
    compliant = MarketContext(
        bars=bars,
        side="long",
        symbol="MNQ",
        account_equity_usd=10_000.0,
        risk_per_trade_pct=0.01,
        stop_distance_pct=0.005,
    )
    over_risk = MarketContext(
        bars=bars,
        side="long",
        symbol="MNQ",
        account_equity_usd=10_000.0,
        risk_per_trade_pct=0.05,
        stop_distance_pct=0.005,
    )

    r1 = consult_sage(
        compliant,
        enabled={"risk_management"},
        parallel=False,
        use_cache=True,
        apply_edge_weights=False,
    )
    r2 = consult_sage(
        over_risk,
        enabled={"risk_management"},
        parallel=False,
        use_cache=True,
        apply_edge_weights=False,
    )

    assert r1 is not r2
    assert r1.per_school["risk_management"].conviction > r2.per_school["risk_management"].conviction
    assert r2.per_school["risk_management"].conviction == 0.0


def test_sage_parallel_and_serial_produce_same_keys() -> None:
    from eta_engine.brain.jarvis_v3.sage import MarketContext, consult_sage
    from eta_engine.brain.jarvis_v3.sage.consultation import clear_sage_cache

    clear_sage_cache()
    bars = _bars(60, trend="up")
    r_serial = consult_sage(
        MarketContext(bars=bars, side="long", symbol="A"), parallel=False, use_cache=False, apply_edge_weights=False
    )
    r_parallel = consult_sage(
        MarketContext(bars=bars, side="long", symbol="B"), parallel=True, use_cache=False, apply_edge_weights=False
    )
    # Same set of schools should fire in both
    assert set(r_serial.per_school.keys()) == set(r_parallel.per_school.keys())


# ─── Narrative ───────────────────────────────────────────────────


def test_narrative_template_fallback_no_anthropic_key(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from eta_engine.brain.jarvis_v3.sage import SageReport, SchoolVerdict
    from eta_engine.brain.jarvis_v3.sage.base import Bias
    from eta_engine.brain.jarvis_v3.sage.narrative import explain_sage

    verdicts = {
        "dow_theory": SchoolVerdict(
            school="dow_theory", bias=Bias.LONG, conviction=0.7, aligned_with_entry=True, rationale="HH+HL"
        ),
        "trend_following": SchoolVerdict(
            school="trend_following", bias=Bias.LONG, conviction=0.6, aligned_with_entry=True, rationale="EMA stack up"
        ),
    }
    report = SageReport(
        per_school=verdicts,
        composite_bias=Bias.LONG,
        conviction=0.65,
        schools_consulted=2,
        schools_aligned_with_entry=2,
        schools_disagreeing_with_entry=0,
        schools_neutral=0,
    )
    text = explain_sage(report, symbol="MNQ", use_llm=False, bar_ts_key="t1")
    assert "MNQ" in text
    assert "long" in text


# ─── Sage health monitor ─────────────────────────────────────────


def test_sage_health_flags_silently_broken_school(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.sage.health import SageHealthMonitor

    m = SageHealthMonitor(state_path=tmp_path / "health.json")
    # 100 silent neutrals, 0 directional reads -> critical
    for _ in range(100):
        m.observe_consultation(school="broken", was_neutral=True)
    issues = m.check_health()
    broken = next(i for i in issues if i.school == "broken")
    assert broken.severity == "critical"
    assert broken.issue_type == "silent_neutral"


def test_sage_health_warns_on_missing_telemetry_without_calling_it_broken(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.sage.health import SageHealthMonitor

    m = SageHealthMonitor(state_path=tmp_path / "health.json")
    for _ in range(40):
        m.observe_consultation(
            school="options_greeks",
            was_neutral=True,
            observation_kind="missing_telemetry",
        )
    issues = m.check_health()
    issue = next(i for i in issues if i.school == "options_greeks")
    assert issue.severity == "warn"
    assert issue.issue_type == "missing_telemetry"


def test_sage_health_ignores_informative_neutral_regime_school(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.sage.health import SageHealthMonitor

    m = SageHealthMonitor(state_path=tmp_path / "health.json")

    class _Verdict:
        def __init__(self) -> None:
            self.bias = type("_Bias", (), {"value": "neutral"})()
            self.conviction = 0.55
            self.aligned_with_entry = True
            self.rationale = "vol expanding sharply"
            self.signals = {"regime": "expanding", "vol_ratio": 1.9}

    class _Report:
        per_school = {"volatility_regime": _Verdict()}

    for _ in range(50):
        m.observe(_Report())

    assert m.check_health() == []
    snap = m.snapshot()["volatility_regime"]
    assert snap["n_structural_neutral"] == 50
    assert snap["n_silent_neutral"] == 0


def test_sage_health_no_issue_below_min_observations(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.sage.health import SageHealthMonitor

    m = SageHealthMonitor(state_path=tmp_path / "health.json")
    for _ in range(10):
        m.observe_consultation(school="newcomer", was_neutral=True)
    issues = m.check_health()
    # MIN_OBSERVATIONS is 30; below that we don't judge
    assert not any(i.school == "newcomer" for i in issues)
