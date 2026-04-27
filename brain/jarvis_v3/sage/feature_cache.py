"""Opt-in feature cache for sage schools (Wave-6, 2026-04-27).

Performance helper. Schools that compute expensive features (EMAs,
pivots, volume profiles, rolling correlation matrices) can opt into a
shared per-call cache so multiple schools don't redo the same work.

Pattern (in a school's analyze method)::

    from eta_engine.brain.jarvis_v3.sage.feature_cache import get_or_compute

    def analyze(self, ctx):
        ema20 = get_or_compute(ctx, "ema_20", lambda: _ema(ctx.closes(), 20))
        pivots = get_or_compute(ctx, "pivot_highs",
                                lambda: _find_pivots(ctx.highs(), kind="high"))
        ...

The cache is bound to ``ctx`` (per-call). When sage consultation
finishes, the cache is dropped with the context.

Backward-compatible: existing schools that don't call get_or_compute
behave exactly as before. The cache is a tiny dict on each ctx.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.brain.jarvis_v3.sage.base import MarketContext

_CTX_CACHE: dict[int, dict[str, Any]] = {}
_LOCK = threading.Lock()


def get_or_compute(ctx: MarketContext, key: str, compute: Callable[[], Any]) -> Any:  # noqa: ANN401
    """Return ``ctx``'s cached value for ``key``, computing it once if absent.

    Cache is keyed by id(ctx) so two different MarketContext instances
    don't share cache. The cache is cleared explicitly via
    ``clear_for_ctx(ctx)`` (typically by the consultation layer after
    every school has run).
    """
    ctx_id = id(ctx)
    with _LOCK:
        bucket = _CTX_CACHE.setdefault(ctx_id, {})
        if key in bucket:
            return bucket[key]
    # Compute outside the lock so the lambda doesn't hold it
    val = compute()
    with _LOCK:
        # Re-check in case another thread computed it concurrently
        bucket = _CTX_CACHE.setdefault(ctx_id, {})
        bucket.setdefault(key, val)
        return bucket[key]


def clear_for_ctx(ctx: MarketContext) -> int:
    """Drop the cache bucket for ``ctx``. Returns number of entries cleared."""
    with _LOCK:
        bucket = _CTX_CACHE.pop(id(ctx), None)
        return len(bucket) if bucket else 0


def cache_size() -> int:
    """Number of distinct ctx instances currently cached. Diagnostic."""
    with _LOCK:
        return len(_CTX_CACHE)


def clear_all() -> int:
    with _LOCK:
        n = len(_CTX_CACHE)
        _CTX_CACHE.clear()
        return n
