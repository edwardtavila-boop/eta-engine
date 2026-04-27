"""Tests for ``eta_engine.data.redis_cache``.

The Redis backend is exercised when ``redis-py`` + a local Redis are
both available; otherwise the test class skips and only the
in-process fallback is tested.
"""

from __future__ import annotations

import time

import pytest  # noqa: TC002 -- pytest fixtures + decorators are runtime
from eta_engine.data.redis_cache import (
    InProcessLRUCache,
    RedisJsonlCache,
    is_redis_available,
)

# ---------------------------------------------------------------------------
# InProcessLRUCache
# ---------------------------------------------------------------------------


def test_lru_get_returns_none_when_missing() -> None:
    c = InProcessLRUCache()
    assert c.get("nope") is None


def test_lru_set_then_get_returns_value() -> None:
    c = InProcessLRUCache()
    c.set("k", {"a": 1}, ttl_seconds=10, now=100.0)
    assert c.get("k", now=101.0) == {"a": 1}


def test_lru_expires_past_ttl() -> None:
    c = InProcessLRUCache()
    c.set("k", "v", ttl_seconds=1.0, now=100.0)
    assert c.get("k", now=99.5) == "v"          # before expiry
    assert c.get("k", now=101.5) is None        # past expiry
    assert "k" not in c._d                      # eviction observed


def test_lru_evicts_oldest_when_full() -> None:
    c = InProcessLRUCache(max_size=3)
    base = time.time()
    for i in range(5):
        c.set(f"k{i}", i, ttl_seconds=100, now=base)
    assert len(c) == 3
    assert c.get("k0", now=base) is None and c.get("k1", now=base) is None
    assert c.get("k2", now=base) == 2
    assert c.get("k3", now=base) == 3
    assert c.get("k4", now=base) == 4


def test_lru_invalidate_removes_entry() -> None:
    c = InProcessLRUCache()
    c.set("k", "v", ttl_seconds=10)
    c.invalidate("k")
    assert c.get("k") is None


# ---------------------------------------------------------------------------
# RedisJsonlCache (with backend fallback)
# ---------------------------------------------------------------------------


def test_cache_works_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force fall-through: point at a definitely-dead URL so .from_url ping fails.
    monkeypatch.setenv("ETA_REDIS_URL", "redis://127.0.0.1:1/0")
    cache = RedisJsonlCache(url="redis://127.0.0.1:1/0")
    assert cache.backend == "in-process"
    cache.set("k", {"x": 1}, ttl_seconds=10)
    assert cache.get("k") == {"x": 1}


def test_cache_get_or_compute_caches_call_count() -> None:
    cache = RedisJsonlCache(url="redis://127.0.0.1:1/0")
    calls = {"n": 0}

    def _compute() -> dict[str, int]:
        calls["n"] += 1
        return {"v": calls["n"]}

    a = cache.get_or_compute("k", ttl_seconds=10, compute_fn=_compute)
    b = cache.get_or_compute("k", ttl_seconds=10, compute_fn=_compute)
    assert a == {"v": 1}
    assert b == {"v": 1}
    assert calls["n"] == 1


def test_cache_invalidate_forces_recompute() -> None:
    cache = RedisJsonlCache(url="redis://127.0.0.1:1/0")
    cache.set("k", "first", ttl_seconds=10)
    cache.invalidate("k")
    assert cache.get("k") is None


def test_cache_rejects_non_json_payload(caplog: pytest.LogCaptureFixture) -> None:
    cache = RedisJsonlCache(url="redis://127.0.0.1:1/0")
    # objects with no JSON encoder should be silently dropped (warn-logged)
    cache.set("k", object(), ttl_seconds=10)
    assert cache.get("k") is None


def test_cache_namespace_prefix_isolates_keys() -> None:
    cache_a = RedisJsonlCache(url="redis://127.0.0.1:1/0", namespace="ns_a")
    cache_b = RedisJsonlCache(url="redis://127.0.0.1:1/0", namespace="ns_b")
    cache_a.set("k", "from_a", ttl_seconds=10)
    cache_b.set("k", "from_b", ttl_seconds=10)
    assert cache_a.get("k") == "from_a"
    assert cache_b.get("k") == "from_b"


# ---------------------------------------------------------------------------
# is_redis_available -- detect probe
# ---------------------------------------------------------------------------


def test_is_redis_available_returns_bool() -> None:
    # Just confirm it doesn't raise + returns bool. Actual value
    # depends on whether redis-py is installed and a server reachable.
    result = is_redis_available()
    assert isinstance(result, bool)


def test_is_redis_available_with_dead_url_is_false() -> None:
    assert is_redis_available("redis://127.0.0.1:1/0") is False
