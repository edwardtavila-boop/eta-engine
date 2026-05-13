"""Mem0 semantic memory layer — replaces Jaccard similarity with embeddings.

Integrates with the Avengers precedent cache to provide embedding-based
semantic search over past trading episodes. Falls back to Jaccard
similarity when Mem0 is not configured.

Requires: ``pip install mem0ai`` and a running Qdrant instance (or
cloud vector store) for production use. For development, an in-memory
FAISS backend works without external dependencies.

Usage::

    from eta_engine.brain.jarvis_v3.mem0_memory import Mem0Memory

    mem = Mem0Memory()
    mem.store(category="trade_close", text="MNQ long breakout +2.4R", ...)
    hits = mem.search("long breakout above resistance high volume")
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class Mem0Memory:
    """Embedding-based semantic memory over trading episodes.

    Wraps Mem0's memory API to store and search trading episodes using
    neural embeddings. Replaces Jaccard token similarity with true
    semantic matching: "bullish breakout" matches "long squeeze above
    resistance" even with zero token overlap.
    """

    def __init__(self, *, user_id: str = "jarvis_trading") -> None:
        self._user_id = user_id
        self._mem = None
        self._available = False
        self._init_attempted = False

    def _ensure_init(self) -> bool:
        if self._init_attempted:
            return self._available
        self._init_attempted = True
        try:
            import mem0  # noqa: F401

            try:
                # Try FAISS in-memory (no external deps needed)
                config = {
                    "vector_store": {
                        "provider": "faiss",
                        "config": {
                            "collection_name": "jarvis_episodes",
                            "embedding_model_dims": 1536,
                            "distance_strategy": "cosine",
                        },
                    },
                    "llm": {"provider": "deepseek", "config": {"model": "deepseek-v4-flash"}},
                    "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
                }
                self._mem = mem0.Memory.from_config(config)
                self._available = True
                logger.info("Mem0 initialized (FAISS in-memory)")
                return True
            except Exception:  # noqa: BLE001
                pass
            try:
                config = {
                    "vector_store": {
                        "provider": "qdrant",
                        "config": {"collection_name": "jarvis_episodes", "host": "localhost", "port": 6333},
                    },
                    "llm": {"provider": "deepseek", "config": {"model": "deepseek-v4-flash"}},
                    "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
                }
                self._mem = mem0.Memory.from_config(config)
                self._available = True
                logger.info("Mem0 initialized (Qdrant)")
                return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("Mem0 unavailable: %s", exc)
        except ImportError:
            logger.debug("mem0ai not installed")
        return False

    def store(
        self,
        *,
        category: str,
        goal: str,
        outcome: bool,
        realized_r: float,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Store one trading episode for future semantic retrieval."""
        if not self._ensure_init():
            return False
        try:
            outcome_label = "SUCCESS" if outcome else "FAILED"
            text = f"[{category}] {outcome_label}: {goal} ({realized_r:+.2f}R)"
            merged = {"category": category, "realized_r": realized_r, **(metadata or {})}
            self._mem.add(text, user_id=self._user_id, metadata=merged)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Mem0 store failed: %s", exc)
            return False

    def search(
        self,
        query: str,
        *,
        k: int = 5,
        min_score: float = 0.25,
    ) -> list[dict[str, Any]]:
        """Semantic search for similar trading episodes.

        Returns episodes with score >= min_score, sorted by similarity.
        Empty list when Mem0 unavailable or no matches found.
        """
        if not self._ensure_init():
            return []
        try:
            results = self._mem.search(query, user_id=self._user_id, limit=k)
            filtered = []
            for r in results:
                score = r.get("score", 0.0)
                if score < min_score:
                    continue
                mem = r.get("memory", "") or ""
                meta = r.get("metadata", {}) or {}
                filtered.append(
                    {
                        "text": mem,
                        "score": score,
                        "category": meta.get("category", ""),
                        "realized_r": meta.get("realized_r", 0.0),
                    }
                )
            return filtered
        except Exception as exc:  # noqa: BLE001
            logger.debug("Mem0 search failed: %s", exc)
            return []

    @property
    def is_available(self) -> bool:
        return self._ensure_init()
