"""
EVOLUTIONARY TRADING ALGO  //  data.sentiment_lunarcrush
============================================
LunarCrush MCP facade. 5-min lru_cache + 10 req/min rate-limit window.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from functools import lru_cache

log = logging.getLogger(__name__)


def _five_min_bucket() -> int:
    return int(time.time() // 300)


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
    """TODO: mcp__lunarcrush__Topic_Time_Series with window_h-hour lookback."""
    _ = (asset, window_h, bucket)
    return {"posts": 0, "interactions": 0, "contributors": 0}


@lru_cache(maxsize=4)
def _fetch_fear_greed_cached(bucket: int) -> int:
    """TODO: call a public fear-greed index or derive from sentiment."""
    _ = bucket
    return 50
