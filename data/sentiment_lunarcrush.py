"""
EVOLUTIONARY TRADING ALGO  //  data.sentiment_lunarcrush
============================================
LunarCrush MCP facade. 5-min lru_cache + 10 req/min rate-limit window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from collections import deque
from functools import lru_cache

from eta_engine.data.market_news import headline_volume_proxy

log = logging.getLogger(__name__)
_ALTERNATIVE_FNG_URL = "https://api.alternative.me/fng/?limit=1&format=json"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124 Safari/537.36"
)


def _five_min_bucket() -> int:
    return int(time.time() // 300)


def _fetch_json(url: str, *, timeout: float = 20.0) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


class LunarCrushClient:
    """Async facade over mcp__lunarcrush__* tools.

    TODO: wire actual mcp__lunarcrush__Cryptocurrencies / Topic_Time_Series etc.
    """

    def __init__(self, rate_limit_per_min: int = 10) -> None:
        self.rate_limit_per_min = rate_limit_per_min
        self._recent_calls: deque[float] = deque(maxlen=rate_limit_per_min)
        self._lock = asyncio.Lock()

    async def fetch_galaxy_score(self, asset: str) -> float:
        await self._gate()
        return _fetch_galaxy_cached(asset, _five_min_bucket())

    async def fetch_alt_rank(self, asset: str) -> int:
        await self._gate()
        return _fetch_alt_rank_cached(asset, _five_min_bucket())

    async def fetch_social_volume(
        self,
        asset: str,
        window_h: int = 24,
    ) -> dict:
        await self._gate()
        return _fetch_social_volume_cached(asset, window_h, _five_min_bucket())

    async def fetch_fear_greed(self) -> int:
        await self._gate()
        return _fetch_fear_greed_cached(_five_min_bucket())

    # ------------------------------------------------------------------
    # Rate limiter: ensure at most N calls per 60 seconds
    # ------------------------------------------------------------------
    async def _gate(self) -> None:
        async with self._lock:
            now = time.time()
            while self._recent_calls and now - self._recent_calls[0] > 60.0:
                self._recent_calls.popleft()
            if len(self._recent_calls) >= self.rate_limit_per_min:
                wait = 60.0 - (now - self._recent_calls[0])
                if wait > 0:
                    log.debug("lunarcrush rate-limit: sleeping %.2fs", wait)
                    await asyncio.sleep(wait)
            self._recent_calls.append(time.time())


# ---------------------------------------------------------------------------
# Module-level lru_cached stubs
# ---------------------------------------------------------------------------


@lru_cache(maxsize=256)
def _fetch_galaxy_cached(asset: str, bucket: int) -> float:
    """TODO: mcp__lunarcrush__Cryptocurrencies -> parse galaxy_score."""
    _ = (asset, bucket)
    return 50.0


@lru_cache(maxsize=256)
def _fetch_alt_rank_cached(asset: str, bucket: int) -> int:
    """TODO: mcp__lunarcrush__Cryptocurrencies -> parse alt_rank."""
    _ = (asset, bucket)
    return 999


@lru_cache(maxsize=256)
def _fetch_social_volume_cached(asset: str, window_h: int, bucket: int) -> dict:
    """Fallback to a public headline-volume proxy until LunarCrush is wired."""
    _ = (asset, window_h, bucket)
    return headline_volume_proxy(asset, window_h=window_h)


@lru_cache(maxsize=4)
def _fetch_fear_greed_cached(bucket: int) -> int:
    """Fetch the public Alternative.me Fear & Greed index."""
    _ = bucket
    payload = _fetch_json(_ALTERNATIVE_FNG_URL)
    if isinstance(payload, dict):
        items = payload.get("data")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    return int(item["value"])
                except (KeyError, TypeError, ValueError):
                    continue
    return 50
