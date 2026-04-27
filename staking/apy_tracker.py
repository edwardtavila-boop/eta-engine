"""DefiLlama APY tracker — live yield feed for staking adapters.

Fetches pool APY from `DefiLlama's Yields API <https://yields.llama.fi>`_.
Rate-limited via 5-min in-memory cache; single aiohttp session shared across
the process lifetime. Callers don't need to close the session — the adapter
does so on ``close()``.

The tracker maps a protocol key (``"lido"``, ``"jito"``, ``"ethena"``, ``"flare"``)
onto a DefiLlama pool-id filter. When the API is unavailable or the pool
isn't matched, the tracker returns ``None`` and the adapter falls back to the
hardcoded ``target_apy`` — the allocator still has a number to work with.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DEFILLAMA_YIELDS_URL = "https://yields.llama.fi/pools"
CACHE_TTL_SECONDS: int = 300  # 5 minutes — respects free-tier rate limits

# Pool-id → APY mapping. These IDs are stable DefiLlama identifiers; the real
# production call filters /pools by project+chain+symbol and picks the winner.
# We use name fragments here for robustness against id rotation.
_POOL_FILTERS: dict[str, dict[str, str]] = {
    "lido": {"project": "lido", "chain": "Ethereum", "symbol": "STETH"},
    "jito": {"project": "jito", "chain": "Solana", "symbol": "JITOSOL"},
    "ethena": {"project": "ethena", "chain": "Ethereum", "symbol": "SUSDE"},
    "flare": {"project": "flare", "chain": "Flare", "symbol": "SFLR"},
}


class ApyTracker:
    """Cached DefiLlama APY lookup. Call :meth:`get_apy` per protocol key."""

    def __init__(self, *, session: aiohttp.ClientSession | None = None) -> None:
        self._session_external = session is not None
        self._session = session
        self._cache: dict[str, tuple[float, float]] = {}  # key -> (apy, fetched_at)
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the underlying aiohttp session, unless externally owned."""
        if self._session is not None and not self._session_external:
            await self._session.close()
            self._session = None

    async def get_apy(self, protocol_key: str) -> float | None:
        """Return live APY for a protocol key, or None if unavailable.

        Cached for :data:`CACHE_TTL_SECONDS`. Network errors return ``None``
        (not exceptions) so adapters can fall back to their hardcoded target.
        """
        key = protocol_key.lower()
        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                apy, fetched_at = cached
                if now - fetched_at < CACHE_TTL_SECONDS:
                    return apy

        apy = await self._fetch_apy(key)
        if apy is not None:
            async with self._lock:
                self._cache[key] = (apy, now)
        return apy

    async def _fetch_apy(self, key: str) -> float | None:
        filt = _POOL_FILTERS.get(key)
        if filt is None:
            logger.warning("ApyTracker: unknown protocol key %r", key)
            return None
        session = await self._get_session()
        try:
            async with session.get(DEFILLAMA_YIELDS_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning("DefiLlama returned %d for %s", resp.status, key)
                    return None
                payload = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as e:
            logger.warning("DefiLlama fetch failed for %s: %s", key, e)
            return None
        except Exception as e:  # noqa: BLE001 - json decode / OS errors
            logger.warning("DefiLlama unexpected error for %s: %s", key, e)
            return None
        return self._match_pool(payload, filt)

    @staticmethod
    def _match_pool(payload: dict[str, Any], filt: dict[str, str]) -> float | None:
        """Filter the /pools response for the best match, return apy percent."""
        pools = payload.get("data") or []
        best: float | None = None
        for p in pools:
            proj = str(p.get("project", "")).lower()
            chain = str(p.get("chain", "")).lower()
            symbol = str(p.get("symbol", "")).upper()
            if filt["project"].lower() in proj and filt["chain"].lower() in chain and filt["symbol"].upper() in symbol:
                apy = p.get("apy")
                if isinstance(apy, (int, float)) and (best is None or apy > best):
                    best = float(apy)
        return best


# Module-level singleton — adapters share it to keep one aiohttp session alive.
_SHARED_TRACKER: ApyTracker | None = None


def get_shared_tracker() -> ApyTracker:
    """Lazily create the process-wide :class:`ApyTracker`."""
    global _SHARED_TRACKER
    if _SHARED_TRACKER is None:
        _SHARED_TRACKER = ApyTracker()
    return _SHARED_TRACKER


async def close_shared_tracker() -> None:
    """Close the shared tracker if it was created — safe to call on shutdown."""
    global _SHARED_TRACKER
    if _SHARED_TRACKER is not None:
        await _SHARED_TRACKER.close()
        _SHARED_TRACKER = None
