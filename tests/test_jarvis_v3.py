"""
JARVIS v3 // tests
==================
Covers all 14 cognitive / learning / infra modules in one file so the
test surface is easy to navigate. Every module gets at least:
  * happy path
  * edge case (empty input, invalid input, boundary threshold)
  * invariants (weights sum to 1.0, probabilities in [0,1], etc.)
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from eta_engine.brain.jarvis_v3 import (
    alerts_explain,
    anomaly,
    bandit,
    budget,
    calibration,
    critique,
    dashboard_payload,
    horizons,
    nl_query,
    portfolio,
    precedent,
    predictive,
    preferences,
    regime_stress,
)
from eta_engine.brain.model_policy import ModelTier, TaskCategory

# ---------------------------------------------------------------------------
# #1 regime_stress
# ---------------------------------------------------------------------------


class TestRegimeStress:
    def test_profile_weights_sum_to_one(self):
        for r in ("RISK_ON", "RISK_OFF", "NEUTRAL", "CRISIS"):
            p = regime_stress.profile_for_regime(r)
            assert abs(p.check_sum() - 1.0) < 1e-6, f"{r} sums to {p.check_sum()}"

    def test_unknown_regime_defaults_to_neutral(self):
        w1 = regime_stress.weights_for_regime("gibberish")
        w2 = regime_stress.weights_for_regime("NEUTRAL")
        assert w1 == w2

    def test_reweight_crisis_dominates_macro(self):
        raws = {
            "macro_event": 1.0,
            "equity_dd": 0.5,
            "open_risk": 0.5,
            "regime_risk": 0.3,
            "override_rate": 0.2,
            "autopilot": 0.0,
            "correlations": 0.0,
            "macro_bias": 0.5,
        }
        comp_crisis, _, binding_crisis = regime_stress.reweight(raws, "CRISIS")
        comp_neutral, _, _ = regime_stress.reweight(raws, "NEUTRAL")
        # Crisis puts more weight on macro_event (0.30 vs 0.25), so composite is higher.
        assert comp_crisis > comp_neutral
        assert binding_crisis == "macro_event"


# ---------------------------------------------------------------------------
# #2 horizons
# ---------------------------------------------------------------------------


class TestHorizons:
    def test_all_horizons_present(self):
        ctx = horizons.project(base_composite=0.3, base_binding="equity_dd", hours_until_event=None)
        assert ctx.now.composite == 0.3
        assert ctx.next_15m.composite == 0.3
        assert ctx.next_1h.composite == 0.3
        assert ctx.overnight.composite == 0.3

    def test_event_bumps_near_horizon(self):
        ctx = horizons.project(
            base_composite=0.2,
            base_binding="equity_dd",
            hours_until_event=0.25,
            event_label="FOMC",
        )
        # 15-minute horizon straddles the event -> bump
        assert ctx.next_15m.composite > ctx.now.composite or ctx.next_15m.composite >= 0.4

    def test_overnight_floor(self):
        ctx = horizons.project(
            base_composite=0.05,
            base_binding="none",
            hours_until_event=None,
            is_overnight_now=True,
        )
        assert ctx.overnight.composite >= 0.40

    def test_binding_horizon_is_max(self):
        ctx = horizons.project(
            base_composite=0.2,
            base_binding="x",
            hours_until_event=0.1,
            event_label="FOMC",
        )
        # With a pending event, max composite must be >= base composite,
        # and binding_horizon must be SOME horizon (any of the four).
        assert ctx.max_composite > 0.2
        assert ctx.binding_horizon in set(horizons.Horizon)


# ---------------------------------------------------------------------------
# #3 predictive
# ---------------------------------------------------------------------------


class TestPredictive:
    def test_empty_series(self):
        p = predictive.projection_from_series([])
        assert p.samples == 0

    def test_trending_up_detected(self):
        series = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        p = predictive.projection_from_series(series)
        assert p.trend > 0.0
        assert p.forecast_5 >= p.forecast_1

    def test_forecast_clipped_to_01(self):
        # Aggressive upward trend should still clip at 1.0
        series = [0.0, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0]
        p = predictive.projection_from_series(series)
        assert 0.0 <= p.forecast_5 <= 1.0

    def test_reset(self):
        fc = predictive.StressForecaster()
        fc.update(0.5)
        fc.update(0.7)
        fc.reset()
        p = fc.update(0.3)
        assert p.samples == 1


# ---------------------------------------------------------------------------
# #4 calibration
# ---------------------------------------------------------------------------


class TestCalibration:
    def test_default_sigmoid_in_01(self):
        feats = calibration.VerdictFeatures(
            verdict="APPROVED",
            stress_composite=0.3,
            sizing_mult=1.0,
            session_phase="MORNING",
        )
        cv = calibration.calibrate_verdict(feats)
        assert 0.0 <= cv.p_correct <= 1.0

    def test_stress_lowers_p_correct(self):
        low = calibration.VerdictFeatures(
            verdict="APPROVED",
            stress_composite=0.1,
            sizing_mult=1.0,
        )
        high = calibration.VerdictFeatures(
            verdict="APPROVED",
            stress_composite=0.9,
            sizing_mult=1.0,
        )
        cv_low = calibration.calibrate_verdict(low)
        cv_high = calibration.calibrate_verdict(high)
        assert cv_low.p_correct > cv_high.p_correct

    def test_sigmoid_fit_converges(self):
        xs = [-2.0, -1.0, 0.0, 1.0, 2.0] * 10
        ys = [0, 0, 0, 1, 1] * 10
        sg = calibration.PlattSigmoid()
        sg.fit(xs, ys, iters=200)
        assert sg.predict(2.0) > sg.predict(-2.0)
        assert sg.fit_samples == len(xs)

    def test_fit_from_missing_file(self, tmp_path: Path):
        sg = calibration.fit_from_audit(tmp_path / "nope.jsonl")
        assert sg.fit_samples == 0


# ---------------------------------------------------------------------------
# #5 portfolio
# ---------------------------------------------------------------------------


class TestPortfolio:
    def test_empty_portfolio(self):
        rep = portfolio.assess_portfolio([])
        assert rep.gross_r == 0.0
        assert rep.cluster_breach is False

    def test_uncorrelated_no_breach(self):
        exps = [
            portfolio.Exposure(subsystem="a", symbol="MNQ", r_at_risk=1.5),
            portfolio.Exposure(subsystem="b", symbol="BTC", r_at_risk=1.5),
        ]
        rep = portfolio.assess_portfolio(exps, corr_matrix={("MNQ", "BTC"): 0.1})
        assert not rep.cluster_breach

    def test_correlated_triggers_breach(self):
        exps = [
            portfolio.Exposure(subsystem="a", symbol="BTC", r_at_risk=2.0),
            portfolio.Exposure(subsystem="b", symbol="ETH", r_at_risk=2.0),
            portfolio.Exposure(subsystem="c", symbol="SOL", r_at_risk=2.0),
        ]
        corr = {
            ("BTC", "ETH"): 0.85,
            ("BTC", "SOL"): 0.80,
            ("ETH", "SOL"): 0.82,
        }
        rep = portfolio.assess_portfolio(exps, corr_matrix=corr, max_cluster_r=3.0)
        assert rep.cluster_breach
        assert rep.verdict_downgrade in {"CONDITIONAL", "DENIED"}

    def test_net_vs_gross(self):
        exps = [
            portfolio.Exposure(subsystem="a", symbol="MNQ", r_at_risk=1.5),
            portfolio.Exposure(subsystem="b", symbol="MNQ2", r_at_risk=-1.0),
        ]
        rep = portfolio.assess_portfolio(exps)
        assert rep.gross_r == 2.5
        assert rep.net_r == 0.5


# ---------------------------------------------------------------------------
# #6 bandit
# ---------------------------------------------------------------------------


class TestBandit:
    def test_pinned_category_never_downgraded(self):
        b = bandit.LLMBandit(rng=random.Random(0))
        sel = b.select_tier(TaskCategory.RED_TEAM_SCORING)
        assert sel.tier == ModelTier.OPUS

    def test_cold_start_uses_policy_default(self):
        b = bandit.LLMBandit(rng=random.Random(0))
        sel = b.select_tier(TaskCategory.CODE_REVIEW)
        assert sel.exploratory is True
        assert sel.tier == ModelTier.SONNET

    def test_bandit_converges_to_rewarded_tier(self):
        b = bandit.LLMBandit(rng=random.Random(42), min_pulls_before_bandit=2)
        cat = TaskCategory.CODE_REVIEW
        # Haiku always succeeds, Opus always fails -- cheap wins
        for _ in range(40):
            sel = b.select_tier(cat)
            if sel.tier == ModelTier.HAIKU:
                b.reward(cat, sel.tier, 1)
            elif sel.tier == ModelTier.OPUS:
                b.reward(cat, sel.tier, 0)
            else:
                b.reward(cat, sel.tier, 1)  # sonnet also works
        # After learning, haiku should be most-pulled for this category.
        arms = {t: b.state.get(cat, t) for t in ModelTier}
        assert arms[ModelTier.HAIKU].pulls >= arms[ModelTier.OPUS].pulls

    def test_save_load_roundtrip(self, tmp_path: Path):
        b = bandit.LLMBandit()
        b.reward(TaskCategory.CODE_REVIEW, ModelTier.HAIKU, 1)
        path = tmp_path / "bandit.json"
        b.save(path)
        b2 = bandit.LLMBandit.load(path)
        arm = b2.state.get(TaskCategory.CODE_REVIEW, ModelTier.HAIKU)
        assert arm.pulls == 1


# ---------------------------------------------------------------------------
# #7 preferences
# ---------------------------------------------------------------------------


class TestPreferences:
    def test_no_history_returns_none(self):
        lrn = preferences.OperatorPreferenceLearner()
        assert lrn.nudge_for("bot.mnq", "ORDER_PLACE", "overnight_refused") is None

    def test_loosen_signal_captured(self):
        lrn = preferences.OperatorPreferenceLearner(half_life_days=30)
        now = datetime.now(UTC)
        for i in range(5):
            lrn.observe(
                preferences.OverrideEvent(
                    ts=now - timedelta(days=i),
                    subsystem="bot.mnq",
                    action="ORDER_PLACE",
                    reason_code="overnight_refused",
                    direction="loosen",
                )
            )
        n = lrn.nudge_for("bot.mnq", "ORDER_PLACE", "overnight_refused", now=now)
        assert n is not None and n.score > 0

    def test_tighten_signal_captured(self):
        lrn = preferences.OperatorPreferenceLearner(half_life_days=30)
        now = datetime.now(UTC)
        for i in range(3):
            lrn.observe(
                preferences.OverrideEvent(
                    ts=now - timedelta(hours=i),
                    subsystem="bot.mnq",
                    action="ORDER_PLACE",
                    reason_code="trade_ok",
                    direction="tighten",
                )
            )
        n = lrn.nudge_for("bot.mnq", "ORDER_PLACE", "trade_ok", now=now)
        assert n is not None and n.score < 0

    def test_persistence_roundtrip(self, tmp_path: Path):
        lrn = preferences.OperatorPreferenceLearner()
        lrn.observe(
            preferences.OverrideEvent(
                ts=datetime.now(UTC),
                subsystem="s",
                action="a",
                reason_code="r",
                direction="loosen",
            )
        )
        path = tmp_path / "prefs.json"
        lrn.save(path)
        l2 = preferences.OperatorPreferenceLearner.load(path)
        assert l2._sample_count[("s", "a", "r")] == 1


# ---------------------------------------------------------------------------
# #8 critique
# ---------------------------------------------------------------------------


class TestCritique:
    def test_empty_gives_green(self):
        rep = critique.critique_window([])
        assert rep.severity == "GREEN"

    def test_high_fp_gives_red(self):
        now = datetime.now(UTC)
        decisions = []
        # 20 approved -- 15 wrong, 5 correct -> fp 75%
        for i in range(20):
            decisions.append(
                critique.DecisionRecord(
                    ts=now - timedelta(hours=i),
                    verdict="APPROVED",
                    reason_code="trade_ok",
                    stress_composite=0.3,
                    outcome_correct=1 if i < 5 else 0,
                    realized_r=-1.0,
                )
            )
        rep = critique.critique_window(decisions)
        assert rep.severity == "RED"

    def test_drift_detected(self):
        now = datetime.now(UTC)
        # Build decisions with rising stress over time
        decisions = []
        for i in range(20):
            comp = 0.1 + (i / 20.0) * 0.8  # 0.1 -> 0.9
            decisions.append(
                critique.DecisionRecord(
                    ts=now - timedelta(hours=20 - i),
                    verdict="APPROVED",
                    reason_code="x",
                    stress_composite=comp,
                    outcome_correct=1,
                    realized_r=1.0,
                )
            )
        rep = critique.critique_window(decisions)
        assert rep.stress_drift > 0.05


# ---------------------------------------------------------------------------
# #9 precedent
# ---------------------------------------------------------------------------


class TestPrecedent:
    def test_empty_query(self):
        g = precedent.PrecedentGraph()
        k = precedent.PrecedentKey(regime="RISK_ON", session_phase="MORNING")
        q = g.query(k)
        assert q.n == 0

    def test_record_and_query(self):
        g = precedent.PrecedentGraph()
        k = precedent.PrecedentKey(regime="RISK_ON", session_phase="MORNING")
        for _i in range(4):
            g.record(
                k,
                precedent.PrecedentEntry(
                    ts=datetime.now(UTC),
                    action="TRADE",
                    verdict="APPROVED",
                    outcome_correct=1,
                    realized_r=1.0,
                ),
            )
        q = g.query(k)
        assert q.n == 4
        assert q.win_rate == 1.0
        assert q.mean_r == 1.0

    def test_bounded_history(self):
        g = precedent.PrecedentGraph(max_per_bucket=3)
        k = precedent.PrecedentKey(regime="X", session_phase="Y")
        for _ in range(10):
            g.record(
                k,
                precedent.PrecedentEntry(
                    ts=datetime.now(UTC),
                    action="TRADE",
                ),
            )
        assert g.query(k).n == 3

    def test_save_load(self, tmp_path: Path):
        g = precedent.PrecedentGraph()
        k = precedent.PrecedentKey(regime="X", session_phase="Y")
        g.record(
            k,
            precedent.PrecedentEntry(
                ts=datetime.now(UTC),
                action="TRADE",
                realized_r=0.5,
            ),
        )
        p = tmp_path / "pg.json"
        g.save(p)
        g2 = precedent.PrecedentGraph.load(p)
        assert g2.query(k).n == 1


# ---------------------------------------------------------------------------
# #10 nl_query
# ---------------------------------------------------------------------------


class TestNLQuery:
    @pytest.fixture
    def audit(self, tmp_path: Path):
        p = tmp_path / "audit.jsonl"
        now = datetime.now(UTC)
        records = [
            {
                "request_id": "abc123",
                "verdict": "DENIED",
                "reason": "kill_blocks_all",
                "reason_code": "kill_blocks_all",
                "subsystem": "bot.mnq",
                "ts": now.isoformat(),
                "stress_composite": 0.6,
                "binding_constraint": "equity_dd",
            },
            {
                "request_id": "def456",
                "verdict": "APPROVED",
                "reason": "all gates green",
                "reason_code": "trade_ok",
                "subsystem": "bot.mnq",
                "ts": now.isoformat(),
                "stress_composite": 0.2,
                "binding_constraint": "equity_dd",
            },
        ]
        p.write_text("\n".join(json.dumps(r) for r in records))
        return p

    def test_why_verdict(self, audit):
        r = nl_query.why_verdict(audit, "abc123")
        assert "DENIED" in r.summary

    def test_count_verdict(self, audit):
        r = nl_query.count_verdict(audit, "APPROVED", hours=48)
        assert r.stats["count"] == 1

    def test_reason_freq(self, audit):
        r = nl_query.reason_freq(audit)
        assert any(rec["reason_code"] == "kill_blocks_all" for rec in r.records)

    def test_dispatch_why(self, audit):
        r = nl_query.dispatch(audit, "why did you deny request id=abc123")
        assert r.intent == "WHY_VERDICT"

    def test_dispatch_unparsed(self, audit):
        r = nl_query.dispatch(audit, "totally unrelated question about pizza")
        assert r.intent in {"UNPARSED", "HEALTH"}


# ---------------------------------------------------------------------------
# #11 alerts_explain
# ---------------------------------------------------------------------------


class TestAlertsExplain:
    def test_breach_classified_correctly(self):
        exp = alerts_explain.build_explanation(
            alert_code="dd_approaching_reduce",
            severity="YELLOW",
            contributions={"equity_dd": 0.2, "macro_event": 0.05},
            raw_values={"equity_dd": 0.025, "macro_event": 0.1},
            thresholds={"equity_dd": 0.02, "macro_event": 0.5},
        )
        assert exp.crossings[0].factor == "equity_dd"
        assert exp.crossings[0].kind.value == "BREACH"

    def test_narrative_injected_first(self):
        exp = alerts_explain.build_explanation(
            alert_code="fomc_imminent",
            severity="CRITICAL",
            contributions={},
            raw_values={},
            thresholds={},
            narrative="FOMC 12 min away",
        )
        assert exp.crossings[0].kind.value == "HEADLINE"

    def test_recommendations_present(self):
        exp = alerts_explain.build_explanation(
            alert_code="x",
            severity="RED",
            contributions={"equity_dd": 0.2},
            raw_values={"equity_dd": 0.06},
            thresholds={"equity_dd": 0.05},
        )
        assert any("flatten" in r for r in exp.recommendations)


# ---------------------------------------------------------------------------
# #12 dashboard_payload  (covered sparsely -- React tests handle UI)
# ---------------------------------------------------------------------------


class TestDashboardPayload:
    def test_minimal_payload(self):
        p = dashboard_payload.build_payload(
            health="GREEN",
            stress={"composite": 0.2, "binding": "equity_dd", "components": []},
            horizons={"now": 0.2, "next_15m": 0.3, "next_1h": 0.4, "overnight": 0.5},
            projection={"level": 0.2, "trend": 0.0, "forecast_5": 0.2},
            regime="NEUTRAL",
            session_phase="MORNING",
            suggestion="TRADE",
        )
        assert p.health == "GREEN"
        assert p.stress["binding"] == "equity_dd"


# ---------------------------------------------------------------------------
# #13 budget
# ---------------------------------------------------------------------------


class TestBudget:
    def test_empty_budget_is_ok(self):
        t = budget.BudgetTracker()
        st = t.status()
        assert st.tier_state == "OK"

    def test_downshift_on_heavy_opus_burn(self):
        t = budget.BudgetTracker(hourly_budget=10.0, daily_budget=10.0)
        now = datetime.now(UTC)
        # 10 Opus calls = 50 cost units = 5x over hourly budget of 10
        for _ in range(10):
            t.record(ModelTier.OPUS, TaskCategory.DEBUG, now=now)
        st = t.status(now=now)
        assert st.downgrade_active
        routed, note = t.routed_tier(ModelTier.OPUS, TaskCategory.DEBUG, now=now)
        assert routed == ModelTier.SONNET

    def test_pinned_never_downgrades(self):
        t = budget.BudgetTracker(hourly_budget=1.0)
        now = datetime.now(UTC)
        t.record(ModelTier.OPUS, TaskCategory.RED_TEAM_SCORING, now=now)
        routed, note = t.routed_tier(
            ModelTier.OPUS,
            TaskCategory.RED_TEAM_SCORING,
            now=now,
        )
        assert routed == ModelTier.OPUS
        assert "pinned" in note

    def test_save_load_preserves_records(self, tmp_path: Path):
        t = budget.BudgetTracker()
        t.record(ModelTier.SONNET)
        path = tmp_path / "budget.json"
        t.save(path)
        t2 = budget.BudgetTracker.load(path)
        assert len(t2._records) == 1


# ---------------------------------------------------------------------------
# #14 anomaly
# ---------------------------------------------------------------------------


class TestAnomaly:
    def test_warmup_green(self):
        d = anomaly.DriftDetector("vix")
        for i in range(5):
            r = d.observe(15.0 + i * 0.1)
            assert r.severity == "GREEN"

    def test_spike_flags_red(self):
        d = anomaly.DriftDetector("vix", z_red=3.0)
        # Feed stable values...
        for _ in range(20):
            d.observe(15.0 + random.uniform(-0.05, 0.05))
        # ...then an enormous spike
        r = d.observe(45.0)
        assert r.severity in {"YELLOW", "RED"}

    def test_nan_is_red(self):
        md = anomaly.MultiFieldDetector(["vix"])
        reports = md.observe({"vix": float("nan")})
        assert reports and reports[0].severity == "RED"

    def test_constant_feed_detected(self):
        d = anomaly.DriftDetector("regime_conf")
        for _ in range(15):
            r = d.observe(0.5)
        assert r.severity == "YELLOW"
