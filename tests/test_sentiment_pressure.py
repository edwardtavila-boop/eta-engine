from __future__ import annotations


def test_summarize_pressure_detects_risk_on_lead_asset() -> None:
    from eta_engine.brain.jarvis_v3.sentiment_pressure import summarize_pressure

    pressure = summarize_pressure(
        [
            {
                "asset": "BTC",
                "fear_greed": 0.58,
                "social_volume_z": 0.4,
                "active_topics": [],
            },
            {
                "asset": "SOL",
                "fear_greed": 0.72,
                "social_volume_z": 2.5,
                "active_topics": ["fomo"],
            },
            {
                "asset": "macro",
                "fear_greed": 0.45,
                "social_volume_z": -0.1,
                "active_topics": ["inflation"],
            },
        ],
    )

    assert pressure["status"] == "risk_on"
    assert pressure["lead_positive_asset"] == "SOL"
    assert pressure["lead_negative_asset"] == "macro"
    assert "inflation" in pressure["summary_line"]


def test_build_sentiment_context_uses_warmed_overlay(monkeypatch) -> None:
    from eta_engine.brain.jarvis_v3 import sentiment_pressure

    snapshots = {
        "BTC": {
            "fear_greed": 0.62,
            "social_volume_z": 1.2,
            "raw_source": "cache",
            "topic_flags": {"fomo": True},
            "extras": {"headline_count": 1, "headlines": [{"headline": "BTC bid"}]},
        },
        "macro": {
            "fear_greed": 0.45,
            "social_volume_z": -0.2,
            "raw_source": "cache",
            "topic_flags": {"inflation": True},
            "extras": {"headline_count": 1, "headlines": [{"headline": "Fed watch"}]},
        },
    }

    monkeypatch.setattr(sentiment_pressure.sentiment_overlay, "current_sentiment", lambda asset: snapshots.get(asset))

    context = sentiment_pressure.build_sentiment_context("MBT1", instrument_class="futures")

    assert context is not None
    assert context["assets"] == ["BTC", "macro"]
    assert context["lead_asset"] == "BTC"
    assert context["pressure"]["lead_positive_asset"] == "BTC"


def test_sentiment_pressure_school_aligns_with_long_entry() -> None:
    from eta_engine.brain.jarvis_v3.sage.base import Bias, MarketContext
    from eta_engine.brain.jarvis_v3.sage.consultation import SCHOOLS
    from eta_engine.brain.jarvis_v3.sage.schools.sentiment_pressure import SentimentPressureSchool
    from eta_engine.brain.jarvis_v3.sentiment_pressure import summarize_pressure

    asset_summaries = [
        {
            "asset": "BTC",
            "fear_greed": 0.68,
            "social_volume_z": 1.8,
            "active_topics": ["fomo"],
        },
        {
            "asset": "macro",
            "fear_greed": 0.5,
            "social_volume_z": 0.0,
            "active_topics": [],
        },
    ]
    ctx = MarketContext(
        bars=[{"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100}],
        side="long",
        symbol="BTCUSD",
        instrument_class="crypto",
        sentiment={"asset_summaries": asset_summaries, "pressure": summarize_pressure(asset_summaries)},
    )

    school = SentimentPressureSchool()
    verdict = school.analyze(ctx)

    assert "sentiment_pressure" in SCHOOLS
    assert school.applies_to(ctx)
    assert verdict.bias == Bias.LONG
    assert verdict.aligned_with_entry is True
    assert verdict.conviction >= 0.45
