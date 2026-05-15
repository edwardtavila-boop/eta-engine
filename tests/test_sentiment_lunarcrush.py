from __future__ import annotations

from eta_engine.data import sentiment_lunarcrush


def test_fetch_fear_greed_cached_uses_public_feed(monkeypatch):
    monkeypatch.setattr(
        sentiment_lunarcrush,
        "_fetch_json",
        lambda url, timeout=20.0: {"data": [{"value": "43"}]},
    )
    sentiment_lunarcrush._fetch_fear_greed_cached.cache_clear()

    assert sentiment_lunarcrush._fetch_fear_greed_cached(123) == 43


def test_fetch_social_volume_cached_uses_headline_proxy(monkeypatch):
    monkeypatch.setattr(
        sentiment_lunarcrush,
        "headline_volume_proxy",
        lambda asset, window_h=24: {
            "posts": 3,
            "interactions": 0,
            "contributors": 2,
            "social_volume": 5,
            "social_volume_baseline": 2,
            "source": "google_news_proxy",
        },
    )
    sentiment_lunarcrush._fetch_social_volume_cached.cache_clear()

    payload = sentiment_lunarcrush._fetch_social_volume_cached("BTC", 24, 456)

    assert payload["source"] == "google_news_proxy"
    assert payload["social_volume"] == 5
    assert payload["social_volume_baseline"] == 2
