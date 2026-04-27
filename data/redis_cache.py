"""
EVOLUTIONARY TRADING ALGO  //  data.redis_cache
==============================================
Optional Redis-backed cache for journal hot-index reads.

The MCC dashboard panels currently re-tail the journal JSONL files on
every render (`scripts.jarvis_dashboard._tail_jsonl`). For files that
grow into the 100s of MB (heartbeat, drift, calibration), that's a
sequential file scan each request. This module gives any caller an
optional drop-in cache layer:

    cache = RedisJsonlCache()
    last = cache.get_or_compute(
        key="forecast.last",
        ttl_seconds=10,
        compute_fn=lambda: _tail_jsonl(FORECAST_PATH, n=1),
    )

Redis is **optional**: the package imports without it, and
``RedisJsonlCache`` falls back to an in-process LRU when redis-py is
absent or the daemon can't connect. Code that uses this cache always
gets a working object -- it just may be slower on cache miss.

Public API
----------

* :class:`RedisJsonlCache`     -- the cache facade (Redis-or-memory).
* :func:`is_redis_available`   -- import + connectivity probe.
* :class:`InProcessLRUCache`   -- explicit fallback (used internally).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)


def is_redis_available(url: str | None = None) -> bool:
    """Probe whether a working Redis is reachable.

    True iff (a) ``redis-py`` imports AND (b) ``PING`` round-trips.
    """
    try:
        import redis  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        r = redis.Redis.from_url(url or _default_url(), socket_timeout=0.5)
        return r.ping()
    except Exception as e:  # noqa: BLE001 -- redis lib raises a wide set
        log.debug("redis ping failed: %s", e)
        return False


def _default_url() -> str:
    return os.environ.get("ETA_REDIS_URL", "redis://127.0.0.1:6379/0")


# ---------------------------------------------------------------------------
# In-process LRU fallback
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    expires_at: float
    payload:    Any  # noqa: ANN401 -- cache values are caller-defined


class InProcessLRUCache:
    """Tiny TTL-aware LRU for when Redis is unavailable.

    Bounded by ``max_size`` entries; oldest evicted on overflow. Not
    thread-safe; one instance per asyncio task.
    """

    def __init__(self, max_size: int = 1024) -> None:
        self._max = max_size
        self._d: OrderedDict[str, _Entry] = OrderedDict()

    def get(self, key: str, now: float | None = None) -> Any | None:  # noqa: ANN401 -- cache values are caller-defined
        n = now if now is not None else time.time()
        e = self._d.get(key)
        if e is None:
            return None
        if e.expires_at <= n:
            self._d.pop(key, None)
            return None
        self._d.move_to_end(key)
        return e.payload

    def set(self, key: str, value: Any, ttl_seconds: float, now: float | None = None) -> None:  # noqa: ANN401 -- cache values are caller-defined
        n = now if now is not None else time.time()
        self._d[key] = _Entry(expires_at=n + ttl_seconds, payload=value)
        self._d.move_to_end(key)
        while len(self._d) > self._max:
            self._d.popitem(last=False)

    def invalidate(self, key: str) -> None:
        self._d.pop(key, None)

    def __len__(self) -> int:
        return len(self._d)


# ---------------------------------------------------------------------------
# RedisJsonlCache
# ---------------------------------------------------------------------------


class RedisJsonlCache:
    """JSON-serializing cache facade with automatic Redis-or-memory fallback.

    Construction never raises -- if Redis isn't reachable, we silently
    fall through to the in-process LRU.

    Values must be JSON-serializable (the cache stores ``json.dumps()``
    output as Redis strings).
    """

    def __init__(
        self,
        url: str | None = None,
        namespace: str = "eta",
        in_process_max: int = 1024,
    ) -> None:
        self._url = url or _default_url()
        self._ns = namespace
        self._fallback = InProcessLRUCache(max_size=in_process_max)
        self._redis: Any = None
        self._redis_available = False
        try:
            import redis  # type: ignore[import-not-found]
            r = redis.Redis.from_url(self._url, socket_timeout=0.5)
            if r.ping():
                self._redis = r
                self._redis_available = True
        except Exception as e:  # noqa: BLE001
            log.debug("RedisJsonlCache: falling back to in-process (%s)", e)

    @property
    def backend(self) -> str:
        """Either ``"redis"`` or ``"in-process"`` -- useful for ops/log."""
        return "redis" if self._redis_available else "in-process"

    def _full_key(self, key: str) -> str:
        return f"{self._ns}:{key}"

    def get(self, key: str) -> Any | None:  # noqa: ANN401 -- cache values are caller-defined
        full = self._full_key(key)
        if self._redis_available:
            try:
                raw = self._redis.get(full)
                if raw is None:
                    return None
                return json.loads(raw)
            except Exception as e:  # noqa: BLE001
                log.warning("RedisJsonlCache get failed; demoting: %s", e)
                self._redis_available = False
        return self._fallback.get(full)

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:  # noqa: ANN401 -- cache values are caller-defined
        full = self._full_key(key)
        try:
            payload = json.dumps(value)
        except (TypeError, ValueError) as e:
            log.warning("RedisJsonlCache set: payload not JSON serializable: %s", e)
            return
        if self._redis_available:
            try:
                self._redis.setex(full, int(max(1, ttl_seconds)), payload)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("RedisJsonlCache set failed; demoting: %s", e)
                self._redis_available = False
        self._fallback.set(full, value, ttl_seconds)

    def invalidate(self, key: str) -> None:
        full = self._full_key(key)
        if self._redis_available:
            try:
                self._redis.delete(full)
            except Exception as e:  # noqa: BLE001
                log.warning("RedisJsonlCache invalidate failed: %s", e)
        self._fallback.invalidate(full)

    def get_or_compute(
        self,
        key: str,
        ttl_seconds: float,
        compute_fn: Callable[[], Any],
    ) -> Any:  # noqa: ANN401 -- cache values are caller-defined
        """Cache-aside helper: return cached value or compute + store."""
        cached = self.get(key)
        if cached is not None:
            return cached
        value = compute_fn()
        self.set(key, value, ttl_seconds)
        return value
