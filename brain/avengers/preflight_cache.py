"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.preflight_cache
=================================================
Short-TTL memoization of JARVIS pre-flight verdicts.

Why this exists
---------------
Every Fleet dispatch invokes JARVIS's ``ActionType.LLM_INVOCATION``
pre-flight. That's fast but it's not free -- pydantic validation +
policy eval + a journal append on every call. When the same caller
fires 10 envelopes in the same category within a minute, there is no
reason to re-evaluate policy 10 times.

This module caches ``(category, caller, action_type)`` -> verdict for
``ttl_seconds``. Any change in the inputs busts the cache. The cache
NEVER caches a DENY -- if policy says no, we want to re-ask next call
so an operator override can take effect immediately.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_admin import ActionResponse


class CacheKey(BaseModel):
    """Triple used to key a cached pre-flight verdict."""

    model_config = ConfigDict(frozen=True)

    category: str
    caller: str
    action_type: str


class CacheEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: str
    ts: datetime
    hit_count: int = Field(ge=0, default=0)


class PreflightCache:
    """LRU-capped, TTL-bounded cache of pre-flight verdicts.

    Parameters
    ----------
    ttl_seconds
        Entries older than this are evicted on read.
    max_entries
        Upper bound on cache size. Oldest entries are evicted.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 60.0,
        max_entries: int = 512,
        clock: callable | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock or (lambda: datetime.now(UTC))
        self._entries: OrderedDict[tuple[str, str, str], CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def _key(self, category: str, caller: str, action_type: str) -> tuple[str, str, str]:
        return (category, caller, action_type)

    def get(
        self,
        *,
        category: str,
        caller: str,
        action_type: str,
    ) -> str | None:
        now = self._clock()
        k = self._key(category, caller, action_type)
        entry = self._entries.get(k)
        if entry is None:
            self._misses += 1
            return None
        if now - entry.ts > timedelta(seconds=self.ttl_seconds):
            self._entries.pop(k, None)
            self._misses += 1
            return None
        # Refresh LRU order.
        self._entries.move_to_end(k)
        self._entries[k] = entry.model_copy(update={"hit_count": entry.hit_count + 1})
        self._hits += 1
        return entry.verdict

    def put(
        self,
        *,
        category: str,
        caller: str,
        action_type: str,
        verdict: str,
    ) -> None:
        # Never cache denials or deferrals -- we want live policy on those.
        if verdict not in {"APPROVE", "APPROVE_WITH_RESTRICTIONS"}:
            return
        k = self._key(category, caller, action_type)
        self._entries[k] = CacheEntry(verdict=verdict, ts=self._clock())
        self._entries.move_to_end(k)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def record(self, *, key_inputs: tuple[str, str, str], response: ActionResponse) -> None:
        """Convenience wrapper for callers that already have an ActionResponse."""
        category, caller, action_type = key_inputs
        self.put(
            category=category,
            caller=caller,
            action_type=action_type,
            verdict=response.verdict.value,
        )

    def stats(self) -> dict[str, int | float]:
        total = self._hits + self._misses
        rate = (self._hits / total) if total else 0.0
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": rate,
            "size": len(self._entries),
        }

    def clear(self) -> None:
        self._entries.clear()
        self._hits = 0
        self._misses = 0


__all__ = [
    "CacheEntry",
    "CacheKey",
    "PreflightCache",
]
