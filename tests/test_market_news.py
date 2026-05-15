from __future__ import annotations

from datetime import UTC, datetime

from eta_engine.data import market_news


def test_fetch_google_news_headlines_parses_rss(monkeypatch):
    sample_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Nasdaq futures rise before inflation data - Reuters</title>
      <link>https://news.google.com/rss/articles/demo-1</link>
      <pubDate>Thu, 14 May 2026 13:31:54 GMT</pubDate>
      <description><![CDATA[
        <a href="https://news.google.com/rss/articles/demo-1">Nasdaq futures rise before inflation data</a>
      ]]></description>
      <source url="https://www.reuters.com">Reuters</source>
    </item>
  </channel>
</rss>
"""

    monkeypatch.setattr(market_news, "_fetch_text", lambda url, timeout=15.0: sample_xml)

    rows = market_news.fetch_google_news_headlines(
        "Nasdaq futures",
        limit=3,
        now=datetime(2026, 5, 14, 16, 0, tzinfo=UTC),
    )

    assert len(rows) == 1
    assert rows[0].headline == "Nasdaq futures rise before inflation data - Reuters"
    assert rows[0].publisher == "Reuters"
    assert rows[0].url == "https://news.google.com/rss/articles/demo-1"
    assert rows[0].snippet.startswith("Nasdaq futures rise before inflation data")


def test_headline_volume_proxy_counts_recent_vs_baseline(monkeypatch):
    now = datetime(2026, 5, 14, 16, 0, tzinfo=UTC)
    rows = [
        market_news.NewsHeadline(
            headline="fresh 1",
            url="https://example.com/1",
            publisher="Reuters",
            published_at_utc=datetime(2026, 5, 14, 15, 0, tzinfo=UTC),
            query="Bitcoin",
        ),
        market_news.NewsHeadline(
            headline="fresh 2",
            url="https://example.com/2",
            publisher="Bloomberg",
            published_at_utc=datetime(2026, 5, 14, 14, 30, tzinfo=UTC),
            query="Bitcoin",
        ),
        market_news.NewsHeadline(
            headline="older 1",
            url="https://example.com/3",
            publisher="Reuters",
            published_at_utc=datetime(2026, 5, 14, 12, 30, tzinfo=UTC),
            query="Bitcoin",
        ),
    ]

    monkeypatch.setattr(market_news, "fetch_google_news_headlines", lambda *args, **kwargs: rows)

    payload = market_news.headline_volume_proxy("BTC", window_h=2, now=now)

    assert payload["source"] == "google_news_proxy"
    assert payload["posts"] == 2
    assert payload["contributors"] == 2
    assert payload["social_volume"] == 4
    assert payload["social_volume_baseline"] == 2
