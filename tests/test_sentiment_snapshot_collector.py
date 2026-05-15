from __future__ import annotations

from datetime import UTC, datetime


def test_persist_runtime_sentiment_snapshots_writes_crypto_and_macro(monkeypatch, tmp_path):
    from eta_engine.scripts import sentiment_snapshot_collector as mod

    async def _fake_fetch(asset: str):
        return {
            "asset": asset,
            "source": "google_news_proxy",
            "fear_greed": 43,
            "social_volume": 12,
            "social_volume_baseline": 6,
            "social_volume_raw": {"posts": 8, "contributors": 4},
        }

    def _fake_headlines(query: str, *, limit: int, max_age_hours: float, now: datetime):
        row = mod.NewsHeadline(
            headline=f"{query} rally eases recession fear",
            url="https://example.com/story",
            publisher="Reuters",
            published_at_utc=datetime(2026, 5, 14, 14, 0, tzinfo=UTC),
            query=query,
            snippet="Markets weigh inflation, Fed, and rally momentum.",
        )
        return [row]

    def _fake_proxy(topic: str, *, window_h: int = 24, now: datetime | None = None):
        return {
            "social_volume": 10,
            "social_volume_baseline": 5,
            "posts": 7,
            "contributors": 3,
            "source": "google_news_proxy",
        }

    monkeypatch.setattr(mod, "fetch_sentiment_snapshot", _fake_fetch)
    monkeypatch.setattr(mod, "fetch_google_news_headlines", _fake_headlines)
    monkeypatch.setattr(mod, "headline_volume_proxy", _fake_proxy)

    payload = mod.persist_runtime_sentiment_snapshots(
        assets=["BTC", "macro"],
        cache_dir=tmp_path,
        now=datetime(2026, 5, 14, 16, 0, tzinfo=UTC),
        status_path=tmp_path / "state" / "collector.json",
    )

    assert payload["status"] == "ok"
    assert payload["ok_count"] == 2
    assert payload["results"]["BTC"]["fear_greed"] == 0.43
    assert payload["results"]["BTC"]["social_volume_z"] == 1.0
    assert payload["results"]["macro"]["raw_source"] == "google_news_proxy"
    assert payload["results"]["macro"]["topic_flags"]["fomc"] is True
    assert (tmp_path / "lunarcrush_btc.json").exists()
    assert (tmp_path / "macro_sentiment.json").exists()


def test_macro_headline_scoring_flags_fear_terms():
    from eta_engine.scripts import sentiment_snapshot_collector as mod

    rows = [
        mod.NewsHeadline(
            headline="Stock market selloff deepens as inflation fear hits yields",
            url="https://example.com/fear",
            publisher="Bloomberg",
            published_at_utc=datetime(2026, 5, 14, 14, 0, tzinfo=UTC),
            query="macro",
            snippet="Fed and CPI remain in focus.",
        )
    ]

    fg = mod._fear_greed_from_headlines(rows)
    flags = mod._topic_flags(rows, macro=True)

    assert fg < 0.5
    assert flags["inflation"] is True
