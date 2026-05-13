"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.precedent_cache
=================================================
Cheap RAG-over-journal lookup that lets the Fleet short-circuit repeated
questions without invoking Claude.

Why this exists
---------------
Every Opus dispatch costs 5x a Sonnet call. If the same
``(category, goal-shape, caller)`` envelope was answered successfully N
times in the last week, the Nth+1 ask shouldn't be spending $0.50 on a
new call -- it should cite precedent and move on.

No embeddings, no vector DB. Just:
  1. Tail the JSONL journal.
  2. Bucket past dispatches by ``(category, caller)``.
  3. For a candidate envelope, find the K most similar by tokenized-goal
     Jaccard similarity.
  4. If all K agree on outcome (APPROVED + non-empty artifact), emit a
     ``SkipVerdict`` telling the caller to reuse the precedent.

Pure stdlib. Readable. Good enough until the ``vector_precedent.py`` in
jarvis_v3/next_level gets wired up properly.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.avengers.base import avengers_journal_read_path

if TYPE_CHECKING:
    from pathlib import Path

    from eta_engine.brain.avengers.base import TaskEnvelope


_TOK_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_TOK_RE.findall(text.lower()))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


class PrecedentHit(BaseModel):
    """One past dispatch that looked like the current envelope."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    category: str
    caller: str
    goal: str
    success: bool
    similarity: float = Field(ge=0.0, le=1.0)
    artifact_snippet: str = ""


class SkipVerdict(BaseModel):
    """Returned when cache says the Fleet can skip a dispatch."""

    model_config = ConfigDict(frozen=True)

    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    reused_artifact: str
    precedents: list[PrecedentHit]


class PrecedentCache:
    """Lazily reads the avengers journal and serves similarity lookups.

    Parameters
    ----------
    journal_path
        JSONL file to tail. Defaults to the canonical Avengers journal,
        with legacy home-journal readback when canonical history is absent.
    lookback_days
        Only considers entries newer than this.
    min_similarity
        Floor for a journal entry to count as "similar". Below this, the
        hit is dropped.
    min_precedents
        Need at least this many similar APPROVED entries to justify a
        skip. 3 is a sensible default -- one coincidence, two correlation,
        three a pattern.
    """

    def __init__(
        self,
        journal_path: Path | None = None,
        *,
        lookback_days: float = 30.0,
        min_similarity: float = 0.55,
        min_precedents: int = 3,
    ) -> None:
        self.journal_path = avengers_journal_read_path(journal_path)
        self.lookback_days = lookback_days
        self.min_similarity = min_similarity
        self.min_precedents = min_precedents

    def _load(self) -> list[dict]:
        if not self.journal_path.exists():
            return []
        # Delta-load: only read lines since last known position.
        # On first call (or journal rotated), re-read from scratch.
        current_size = self.journal_path.stat().st_size
        cached_size = getattr(self, "_journal_size", 0)
        cached_rows: list[dict] = getattr(self, "_cached_rows", [])
        if cached_rows and current_size >= cached_size and current_size > 0:
            if current_size == cached_size:
                return cached_rows
            # Incremental: read only new bytes
            with self.journal_path.open("r", encoding="utf-8") as f:
                f.seek(cached_size)
                tail = f.read()
            self._cached_rows = cached_rows + self._parse_lines(tail)
            self._journal_size = current_size
            return self._cached_rows
        # Full load
        text = self.journal_path.read_text(encoding="utf-8")
        rows = self._parse_lines(text)
        self._cached_rows = rows
        self._journal_size = current_size
        return rows

    def _parse_lines(self, text: str) -> list[dict]:
        cutoff = datetime.now(UTC) - timedelta(days=self.lookback_days)
        out: list[dict] = []
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") == "heartbeat":
                continue
            env = rec.get("envelope") or {}
            res = rec.get("result") or {}
            if not env or not res:
                continue
            ts_raw = rec.get("ts") or env.get("ts")
            try:
                ts = datetime.fromisoformat(
                    str(ts_raw).replace("Z", "+00:00"),
                )
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                continue
            if ts < cutoff:
                continue
            out.append(
                {
                    "ts": ts,
                    "category": env.get("category", ""),
                    "caller": env.get("caller", ""),
                    "goal": env.get("goal", ""),
                    "success": bool(res.get("success", False)),
                    "artifact": res.get("artifact", "") or "",
                }
            )
        return out

    def _mem0_lookup(self, envelope: TaskEnvelope, *, k: int) -> list[PrecedentHit] | None:
        """Try Mem0 semantic search. Returns None if unavailable."""
        try:
            from eta_engine.brain.jarvis_v3.mem0_memory import Mem0Memory

            mem0 = Mem0Memory()
            results = mem0.search(envelope.goal, k=k)
            if not results:
                return None
            hits = []
            for r in results:
                hits.append(
                    PrecedentHit(
                        ts=r.get("ts", ""),
                        category=r.get("category", envelope.category.value),
                        caller="mem0",
                        goal=r.get("text", ""),
                        success=True,
                        similarity=r.get("score", 0.0),
                        artifact_snippet=r.get("text", "")[:500],
                    )
                )
            return hits
        except Exception:  # noqa: BLE001
            return None

    def lookup(self, envelope: TaskEnvelope, *, k: int = 5) -> list[PrecedentHit]:
        # Try Mem0 semantic search first, fall back to Jaccard
        mem0_hits = self._mem0_lookup(envelope, k=k)
        if mem0_hits:
            return mem0_hits[:k]

        target_tokens = _tokens(envelope.goal)
        hits: list[PrecedentHit] = []
        for rec in self._load():
            if rec["category"] != envelope.category.value:
                continue
            sim = _jaccard(target_tokens, _tokens(rec["goal"]))
            if sim < self.min_similarity:
                continue
            hits.append(
                PrecedentHit(
                    ts=rec["ts"],
                    category=rec["category"],
                    caller=rec["caller"],
                    goal=rec["goal"],
                    success=rec["success"],
                    similarity=sim,
                    artifact_snippet=rec["artifact"][:500],
                )
            )
        hits.sort(key=lambda h: h.similarity, reverse=True)
        return hits[:k]

    def should_skip(self, envelope: TaskEnvelope) -> SkipVerdict | None:
        hits = self.lookup(envelope, k=max(5, self.min_precedents))
        if len(hits) < self.min_precedents:
            return None
        winners = [h for h in hits if h.success]
        if len(winners) < self.min_precedents:
            return None
        # All top-K succeeded. Reuse the freshest artifact.
        freshest = max(winners, key=lambda h: h.ts)
        avg_sim = sum(w.similarity for w in winners) / len(winners)
        return SkipVerdict(
            confidence=min(0.99, avg_sim),
            reason=(
                f"precedent: {len(winners)} successful matches in last "
                f"{int(self.lookback_days)}d, avg similarity={avg_sim:.2f}"
            ),
            reused_artifact=freshest.artifact_snippet,
            precedents=winners,
        )


__all__ = [
    "PrecedentCache",
    "PrecedentHit",
    "SkipVerdict",
]
