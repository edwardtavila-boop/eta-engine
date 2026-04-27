"""
JARVIS v3 // next_level.vector_precedent
========================================
Vector-memory RAG precedent.

v3 precedent.PrecedentGraph is exact-tuple keyed. Useful, but misses
semantic neighbors: a (CRISIS, MORNING, FOMC) bucket shouldn't be blind
to what (CRISIS, MORNING, CPI) looked like.

This module upgrades to a vector index built from feature embeddings.
No ML library needed -- we use a deterministic hash-based feature
embedding (bag-of-tokens + numeric binning) and cosine similarity.
When you need real embeddings (e.g. sentence-transformers), swap
``_embed`` for an actual model.

Pure stdlib + pydantic.
"""

from __future__ import annotations

import json
import math
from datetime import datetime  # noqa: TC003  (pydantic runtime)
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable

EMBEDDING_DIM = 128


class PrecedentVectorEntry(BaseModel):
    """One past event stored as a vector + metadata."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    ts: datetime
    regime: str
    session_phase: str
    event_category: str = ""
    binding_constraint: str = ""
    tags: list[str] = Field(default_factory=list)
    action: str = "UNKNOWN"
    outcome_correct: int | None = None
    realized_r: float | None = None
    vector: list[float] = Field(min_length=EMBEDDING_DIM, max_length=EMBEDDING_DIM)
    note: str = ""


class NeighborResult(BaseModel):
    """One retrieved neighbor in a query response."""

    model_config = ConfigDict(frozen=True)

    entry: PrecedentVectorEntry
    similarity: float = Field(ge=-1.0, le=1.0)


class PrecedentSynthesis(BaseModel):
    """Aggregated synthesis of a k-NN query."""

    model_config = ConfigDict(frozen=True)

    n_neighbors: int = Field(ge=0)
    mean_similarity: float
    hit_rate: float | None = None
    mean_r: float | None = None
    top_actions: list[tuple[str, int]]
    suggestion: str


class VectorPrecedentStore:
    """Hash-based feature vectorizer + cosine k-NN retrieval.

    The vectorizer is deterministic so identical inputs always map to the
    identical vector. Good enough for small corpora (thousands of
    decisions) without a heavyweight embedding model.
    """

    def __init__(self) -> None:
        self._entries: dict[str, PrecedentVectorEntry] = {}

    def record(
        self,
        *,
        entry_id: str,
        ts: datetime,
        regime: str,
        session_phase: str,
        event_category: str = "",
        binding_constraint: str = "",
        tags: list[str] | None = None,
        action: str = "UNKNOWN",
        outcome_correct: int | None = None,
        realized_r: float | None = None,
        numeric_features: dict[str, float] | None = None,
        note: str = "",
    ) -> PrecedentVectorEntry:
        vec = _embed(
            regime=regime,
            session_phase=session_phase,
            event_category=event_category,
            binding_constraint=binding_constraint,
            tags=tags or [],
            numeric_features=numeric_features or {},
        )
        entry = PrecedentVectorEntry(
            id=entry_id,
            ts=ts,
            regime=regime,
            session_phase=session_phase,
            event_category=event_category,
            binding_constraint=binding_constraint,
            tags=tags or [],
            action=action,
            outcome_correct=outcome_correct,
            realized_r=realized_r,
            vector=vec,
            note=note,
        )
        self._entries[entry_id] = entry
        return entry

    def search(
        self,
        *,
        regime: str,
        session_phase: str,
        event_category: str = "",
        binding_constraint: str = "",
        tags: list[str] | None = None,
        numeric_features: dict[str, float] | None = None,
        k: int = 5,
    ) -> list[NeighborResult]:
        if not self._entries:
            return []
        q = _embed(
            regime=regime,
            session_phase=session_phase,
            event_category=event_category,
            binding_constraint=binding_constraint,
            tags=tags or [],
            numeric_features=numeric_features or {},
        )
        scored: list[NeighborResult] = []
        for e in self._entries.values():
            sim = _cosine(q, e.vector)
            scored.append(NeighborResult(entry=e, similarity=round(sim, 6)))
        scored.sort(key=lambda r: r.similarity, reverse=True)
        return scored[:k]

    def synthesize(
        self,
        neighbors: Iterable[NeighborResult],
    ) -> PrecedentSynthesis:
        """Combine k-NN results into a single verdict hint."""
        lst = list(neighbors)
        if not lst:
            return PrecedentSynthesis(
                n_neighbors=0,
                mean_similarity=0.0,
                hit_rate=None,
                mean_r=None,
                top_actions=[],
                suggestion="no neighbors -- proceed with baseline",
            )
        sim_mean = sum(n.similarity for n in lst) / len(lst)
        labeled = [n for n in lst if n.entry.outcome_correct is not None]
        hit_rate = (
            sum(n.entry.outcome_correct for n in labeled if n.entry.outcome_correct) / len(labeled) if labeled else None
        )
        rs = [n.entry.realized_r for n in lst if n.entry.realized_r is not None]
        mean_r = (sum(rs) / len(rs)) if rs else None
        action_counts: dict[str, int] = {}
        for n in lst:
            action_counts[n.entry.action] = action_counts.get(n.entry.action, 0) + 1
        top_actions = sorted(
            action_counts.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )[:3]
        if mean_r is not None and mean_r > 0.3 and (hit_rate or 0) >= 0.55:
            sugg = f"semantic precedent favors TRADE (sim={sim_mean:.2f}, mean_r={mean_r:+.2f})"
        elif mean_r is not None and mean_r < -0.3:
            sugg = f"semantic precedent unfavorable (sim={sim_mean:.2f}, mean_r={mean_r:+.2f}) -- stand aside"
        else:
            sugg = f"mixed precedent (n={len(lst)}, sim={sim_mean:.2f})"
        return PrecedentSynthesis(
            n_neighbors=len(lst),
            mean_similarity=round(sim_mean, 4),
            hit_rate=round(hit_rate, 4) if hit_rate is not None else None,
            mean_r=round(mean_r, 4) if mean_r is not None else None,
            top_actions=top_actions,
            suggestion=sugg,
        )

    def size(self) -> int:
        return len(self._entries)

    # Persistence -------------------------------------------------------
    def save(self, path: Path | str) -> None:
        data = {"entries": [e.model_dump(mode="json") for e in self._entries.values()]}
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> VectorPrecedentStore:
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        inst = cls()
        for e in data.get("entries", []):
            entry = PrecedentVectorEntry.model_validate(e)
            inst._entries[entry.id] = entry
        return inst


# ---------------------------------------------------------------------------
# Deterministic hash-based embedding
# ---------------------------------------------------------------------------


def _embed(
    *,
    regime: str,
    session_phase: str,
    event_category: str,
    binding_constraint: str,
    tags: list[str],
    numeric_features: dict[str, float],
) -> list[float]:
    """Bag-of-tokens + numeric bucketing -> deterministic vector.

    Not as rich as a real embedding model, but:
      * deterministic (same input -> same vector)
      * no dependencies
      * good enough signal on regime/session/event overlap

    To upgrade to real embeddings, replace this body with a call to
    sentence-transformers / Claude embeddings / etc.
    """
    v = [0.0] * EMBEDDING_DIM
    tokens = [
        f"regime:{regime.upper()}",
        f"session:{session_phase.upper()}",
        f"event:{event_category.lower()}",
        f"binding:{binding_constraint.lower()}",
    ]
    tokens.extend(f"tag:{t.lower()}" for t in tags)
    for t in tokens:
        idx = _stable_hash(t) % EMBEDDING_DIM
        v[idx] += 1.0
    # Numeric features into dedicated buckets
    for name, val in numeric_features.items():
        # Clip to [-3, 3] z-score-ish range, map to 8 buckets
        b = max(-3.0, min(3.0, val))
        bucket = int((b + 3.0) / 6.0 * 7.99)  # 0..7
        idx = _stable_hash(f"num:{name}:{bucket}") % EMBEDDING_DIM
        v[idx] += 1.0
    # L2 normalize
    norm = math.sqrt(sum(x * x for x in v))
    if norm > 0:
        v = [x / norm for x in v]
    return v


def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=True))
    return num  # both are already unit-norm -> dot is cosine


def _stable_hash(s: str) -> int:
    h = 0
    for ch in s:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return h
