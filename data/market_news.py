"""Public market-news helpers for non-price intelligence signals.

The ETA workspace already has operator-curated events and broker-side price
truth. This module adds a low-friction public-news layer that can be used to:

* backfill canonical ``record_type="news"`` rows into the symbol-intel lake
* provide a conservative headline-volume proxy for sentiment features when
  social APIs are unavailable

All network calls are best-effort and fail closed to empty results.
"""

from __future__ import annotations

import contextlib
import html
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

_GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124 Safari/537.36"
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

_NEWS_QUERIES: dict[str, str] = {
    "MNQ": "Nasdaq futures OR Nasdaq 100 OR US tech stocks",
    "NQ": "Nasdaq futures OR Nasdaq 100 OR US tech stocks",
    "ES": "S&P 500 futures OR stock market OR Federal Reserve",
    "MES": "S&P 500 futures OR stock market OR Federal Reserve",
    "YM": "Dow futures OR treasury yields OR industrial stocks",
    "MYM": "Dow futures OR treasury yields OR industrial stocks",
    "6E": "euro dollar exchange rate OR ECB OR eurozone inflation",
    "CL": "crude oil futures OR OPEC OR EIA crude inventories",
    "MCL": "crude oil futures OR OPEC OR EIA crude inventories",
    "NG": "natural gas futures OR LNG OR EIA natural gas storage",
    "GC": "gold futures OR treasury yields OR Federal Reserve",
    "MGC": "gold futures OR treasury yields OR Federal Reserve",
    "BTC": "Bitcoin cryptocurrency OR crypto regulation OR ETF flows",
    "MBT": "Bitcoin cryptocurrency OR crypto regulation OR ETF flows",
    "ETH": "Ethereum cryptocurrency OR crypto regulation OR ETF flows",
    "MET": "Ethereum cryptocurrency OR crypto regulation OR ETF flows",
    "SOL": "Solana cryptocurrency OR crypto market structure",
    "XRP": "XRP Ripple SEC lawsuit OR crypto regulation",
}


@dataclass(frozen=True)
class NewsHeadline:
    headline: str
    url: str
    publisher: str
    published_at_utc: datetime
    query: str
    snippet: str = ""
    provider: str = "google_news_rss"


def _normalize_topic_key(raw: str) -> str:
    symbol = str(raw or "").upper().strip()
    if symbol.endswith("1") and len(symbol) > 2:
        symbol = symbol[:-1]
    return symbol


def query_for_symbol(raw: str) -> str | None:
    return _NEWS_QUERIES.get(_normalize_topic_key(raw))


def build_google_news_rss_url(query: str) -> str:
    params = urllib.parse.urlencode(
        {
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
    )
    return f"{_GOOGLE_NEWS_RSS_URL}?{params}"


def _fetch_text(url: str, *, timeout: float = 15.0) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return None


def _strip_html(raw: str) -> str:
    cleaned = _HTML_TAG_RE.sub(" ", raw or "")
    return " ".join(html.unescape(cleaned).split())


def _parse_pubdate(raw: str | None) -> datetime | None:
    if not raw:
        return None
    with contextlib.suppress(TypeError, ValueError, IndexError):
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    return None


def fetch_google_news_headlines(
    query: str,
    *,
    limit: int = 10,
    max_age_hours: float | None = 48.0,
    now: datetime | None = None,
) -> list[NewsHeadline]:
    if not query.strip():
        return []
    xml_text = _fetch_text(build_google_news_rss_url(query))
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    cutoff = now - timedelta(hours=max_age_hours) if max_age_hours is not None else None
    items = root.findall("./channel/item")
    out: list[NewsHeadline] = []
    for item in items:
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        publisher = (item.findtext("source") or "").strip()
        published_at = _parse_pubdate(item.findtext("pubDate"))
        snippet = _strip_html(item.findtext("description") or "")
        if not title or not url or published_at is None:
            continue
        if cutoff is not None and published_at < cutoff:
            continue
        out.append(
            NewsHeadline(
                headline=html.unescape(title),
                url=url,
                publisher=publisher or "unknown",
                published_at_utc=published_at,
                query=query,
                snippet=snippet[:280],
            )
        )
        if len(out) >= max(limit, 0):
            break
    return out


def headline_volume_proxy(
    topic: str,
    *,
    window_h: int = 24,
    now: datetime | None = None,
) -> dict[str, int | str]:
    """Return a conservative headline-volume proxy for a topic.

    This is not a true social stream. The payload is explicitly labeled as a
    Google News proxy so downstream consumers can treat it as a non-price
    narrative intensity signal rather than a social-network count.
    """

    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    query = query_for_symbol(topic) or str(topic).strip()
    headlines = fetch_google_news_headlines(
        query,
        limit=25,
        max_age_hours=max(48.0, float(window_h) * 3.0),
        now=now,
    )
    if not headlines:
        return {
            "posts": 0,
            "interactions": 0,
            "contributors": 0,
            "social_volume": 0,
            "social_volume_baseline": 1,
            "headline_count_window": 0,
            "headline_count_baseline": 0,
            "source": "google_news_proxy",
            "query": query,
        }

    recent_cutoff = now - timedelta(hours=max(window_h, 1))
    prior_cutoff = now - timedelta(hours=max(window_h * 2, 2))
    recent = [row for row in headlines if row.published_at_utc >= recent_cutoff]
    prior = [row for row in headlines if prior_cutoff <= row.published_at_utc < recent_cutoff]
    recent_publishers = {row.publisher for row in recent}
    prior_publishers = {row.publisher for row in prior}
    recent_volume = len(recent) + len(recent_publishers)
    baseline_volume = len(prior) + len(prior_publishers)
    return {
        "posts": len(recent),
        "interactions": 0,
        "contributors": len(recent_publishers),
        "social_volume": recent_volume,
        "social_volume_baseline": max(1, baseline_volume),
        "headline_count_window": len(recent),
        "headline_count_baseline": len(prior),
        "source": "google_news_proxy",
        "query": query,
    }
