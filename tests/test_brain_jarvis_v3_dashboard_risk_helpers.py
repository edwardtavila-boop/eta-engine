from __future__ import annotations

from datetime import UTC, datetime

from eta_engine.brain.jarvis_v3.alerts_explain import CrossingKind, build_explanation
from eta_engine.brain.jarvis_v3.dashboard_payload import build_payload
from eta_engine.brain.jarvis_v3.predictive import StressForecaster, projection_from_series
from eta_engine.brain.jarvis_v3.regime_stress import (
    profile_for_regime,
    reweight,
    weights_for_regime,
)


def test_dashboard_payload_preserves_clock_and_sane_defaults() -> None:
    now = datetime(2026, 4, 29, 15, 0, tzinfo=UTC)

    payload = build_payload(
        health="GREEN",
        stress={"composite": 0.18, "binding": "equity_dd", "components": []},
        horizons={"now": 0.18, "next_15m": 0.21, "next_1h": 0.24, "overnight": 0.3},
        projection={"level": 0.18, "trend": 0.01, "forecast_5": 0.23},
        regime="NEUTRAL",
        session_phase="RTH",
        suggestion="TRADE",
        now=now,
    )

    assert payload.ts == now
    assert payload.recent_verdicts == []
    assert payload.active_gates == []
    assert payload.budget == {}
    assert payload.critique_flags == []
    assert payload.precedent_hint == ""


def test_alert_explanation_orders_crossings_and_recommends_actions() -> None:
    now = datetime(2026, 4, 29, 15, 5, tzinfo=UTC)

    explanation = build_explanation(
        alert_code="risk_stack_red",
        severity="RED",
        contributions={"equity_dd": 0.42, "macro_event": 0.3, "override_rate": 0.2},
        raw_values={"equity_dd": 0.08, "macro_event": 0.9, "override_rate": 0.8},
        thresholds={"equity_dd": 0.05, "macro_event": 0.7, "override_rate": 0.75},
        now=now,
    )

    assert [c.factor for c in explanation.crossings] == [
        "equity_dd",
        "macro_event",
        "override_rate",
    ]
    assert {c.kind for c in explanation.crossings} == {CrossingKind.BREACH}
    assert explanation.built_at == now
    assert "Primary: equity_dd" in explanation.summary
    assert any("flatten open risk" in rec for rec in explanation.recommendations)
    assert any("stand aside" in rec for rec in explanation.recommendations)
    assert any("override log" in rec for rec in explanation.recommendations)


def test_predictive_forecaster_clips_trend_and_empty_series() -> None:
    forecaster = StressForecaster(alpha=1.0, beta=1.0)

    first = forecaster.update(0.2)
    second = forecaster.update(0.5)
    third = forecaster.update(1.7)

    assert first.note == "stress flat"
    assert second.trend == 0.3
    assert second.forecast_5 == 1.0
    assert third.level == 1.0
    assert third.forecast_1 == 1.0
    assert "worsening" in third.note
    assert projection_from_series([]).note == "empty series"


def test_regime_stress_profiles_normalize_aliases_and_binding() -> None:
    risk_on = weights_for_regime("trend-up")
    neutral = weights_for_regime("gibberish")
    profile = profile_for_regime("bear")

    assert risk_on["override_rate"] > neutral["override_rate"]
    assert profile.regime == "RISK_OFF"
    assert abs(profile.check_sum() - 1.0) < 1e-6

    composite, contributions, binding = reweight(
        {"macro_event": 1.0, "equity_dd": 0.25, "override_rate": 0.1},
        "CRISIS",
    )

    assert binding == "macro_event"
    assert composite == sum(contributions.values())
    assert contributions["macro_event"] == 0.3
