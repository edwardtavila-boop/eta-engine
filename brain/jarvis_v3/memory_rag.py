"""Full-build RAG retrieval over hierarchical memory (Wave-10 #3).

The lean ``memory_hierarchy.py`` does retrieval by 4-dim regime/
session/stress/direction cosine. That works but loses information
about the FREE-TEXT narrative attached to each episode. This module
adds:

  * HASH-EMBEDDING of episode narratives (deterministic, pure stdlib,
    8K-dim sparse vector via word-hashing). Approximates a real text
    embedding well enough for "find me episodes that mention 'liquidity
    sweep'" without needing sentence-transformers
  * SUMMARIZATION of N similar episodes into a single operator-readable
    paragraph (extractive: highest-frequency content words)
  * RAG ENRICHMENT: take a current decision context, retrieve top-K
    similar episodes, summarize, return the augmented context that
    JARVIS / firm board can consult
  * IMPORTANCE-SCORED forgetting: when storage exceeds ceiling, evict
    episodes by inverse-IDF (keep the unusual ones; drop the
    repetitive ones)

This is a real upgrade: the lean module retrieves, this one
RETRIEVES + UNDERSTANDS + SUMMARIZES. It is what the audit list
called "RAG" -- retrieval-augmented generation -- minus the actual
LLM generation step (which we leave as a deterministic template
since no LLM dep is acceptable on the hot path).

Use case (hot path: pre-trade context augmentation):

    from eta_engine.brain.jarvis_v3.memory_rag import (
        rag_enrich_decision_context,
    )

    context = rag_enrich_decision_context(
        current_narrative="EMA stack aligned, sage approved, stress 0.4",
        regime="bullish_low_vol", session="rth", stress=0.4,
        direction="long",
        memory=hierarchical_memory,
        k=5,
    )
    print(context.summary)
    print(context.cautions)   # any analog episodes that lost
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import (
        Episode,
        HierarchicalMemory,
    )

logger = logging.getLogger(__name__)


# ─── Hash-embedding (sparse vector of fixed dimension) ────────────


_EMBED_DIM = 8192
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]+")
_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "by",
        "as",
        "is",
        "was",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "from",
        "into",
        "if",
        "when",
        "than",
        "then",
        "there",
        "are",
        "we",
        "you",
        "i",
        "he",
        "she",
        "they",
        "his",
        "her",
        "their",
        "our",
        "your",
        "my",
    }
)


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    lower = text.lower()
    return [t for t in _TOKEN_RE.findall(lower) if t not in _STOP_WORDS]


def _hash_token(tok: str, dim: int = _EMBED_DIM) -> int:
    h = hashlib.md5(tok.encode("utf-8"), usedforsecurity=False).digest()
    return int.from_bytes(h[:4], "big") % dim


def hash_embed(text: str, *, dim: int = _EMBED_DIM) -> dict[int, float]:
    """Return a sparse {index -> count/sqrt(N)} feature dict.

    Length-normalized so cosine over hash-embeddings is meaningful
    even between long and short narratives."""
    tokens = _tokenize(text)
    if not tokens:
        return {}
    counts: dict[int, int] = {}
    for t in tokens:
        idx = _hash_token(t, dim=dim)
        counts[idx] = counts.get(idx, 0) + 1
    norm = math.sqrt(sum(c * c for c in counts.values()))
    if norm == 0:
        return {}
    return {idx: count / norm for idx, count in counts.items()}


def cosine_sparse(a: dict[int, float], b: dict[int, float]) -> float:
    if not a or not b:
        return 0.0
    # Iterate over the smaller dict for efficiency
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


# ─── Retrieval ────────────────────────────────────────────────────


@dataclass
class RetrievedEpisode:
    """Episode plus its retrieval score."""

    episode: Episode
    score: float
    rank: int


def retrieve_similar(
    *,
    query_text: str,
    regime: str,
    session: str,
    stress: float,
    direction: str,
    memory: HierarchicalMemory,
    k: int = 5,
    text_weight: float = 0.7,
    structural_weight: float = 0.3,
) -> list[RetrievedEpisode]:
    """Hybrid retrieval: text similarity (hash-embedding cosine) +
    structural similarity (regime/session/stress/direction match).

    The two are linearly combined; default weights favor the narrative
    text, with structure as a tiebreaker."""
    from eta_engine.brain.jarvis_v3.memory_hierarchy import Episode

    if not memory._episodes:
        return []
    q_emb = hash_embed(query_text)
    probe = Episode(
        ts="",
        signal_id="probe",
        regime=regime,
        session=session,
        stress=stress,
        direction=direction,
        realized_r=0.0,
    )
    probe_struct = probe.feature_vector()

    scored: list[tuple[float, Episode]] = []
    for ep in memory._episodes:
        text_sim = cosine_sparse(q_emb, hash_embed(ep.narrative))
        struct_sim = _struct_cosine(probe_struct, ep.feature_vector())
        total = text_weight * text_sim + structural_weight * struct_sim
        scored.append((total, ep))

    scored.sort(key=lambda t: t[0], reverse=True)
    out: list[RetrievedEpisode] = []
    for rank, (s, ep) in enumerate(scored[:k]):
        out.append(RetrievedEpisode(episode=ep, score=round(s, 4), rank=rank))
    return out


def _struct_cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ─── Extractive summarization ─────────────────────────────────────


def summarize_episodes(
    episodes: list[Episode],
    *,
    max_phrases: int = 5,
) -> str:
    """Return a one-paragraph summary of the most-frequent content
    words and phrases across the episodes' narratives.

    This is EXTRACTIVE -- no LLM, just bag-of-words pattern surfacing.
    Useful for surfacing "what did these analog trades have in common"
    to the operator narrative."""
    if not episodes:
        return ""

    avg_r = sum(e.realized_r for e in episodes) / len(episodes)
    n_pos = sum(1 for e in episodes if e.realized_r > 0)
    win_rate = n_pos / len(episodes)

    counter: Counter[str] = Counter()
    for ep in episodes:
        counter.update(_tokenize(ep.narrative))
    top_phrases = [w for w, _ in counter.most_common(max_phrases)]

    if top_phrases:
        return (
            f"{len(episodes)} analog episodes: avg {avg_r:+.2f}R, "
            f"win-rate {win_rate:.0%}; common phrases: "
            f"{', '.join(top_phrases)}."
        )
    return f"{len(episodes)} analog episodes: avg {avg_r:+.2f}R, win-rate {win_rate:.0%}; no narrative content."


# ─── RAG enrichment (the public entry point) ──────────────────────


@dataclass
class EnrichedContext:
    """Retrieved-and-summarized analog context for a current decision."""

    n_retrieved: int
    summary: str
    cautions: list[str] = field(default_factory=list)
    boosts: list[str] = field(default_factory=list)
    avg_analog_r: float = 0.0
    win_rate_analogs: float = 0.0
    retrieved: list[RetrievedEpisode] = field(default_factory=list)


def rag_enrich_decision_context(
    *,
    current_narrative: str,
    regime: str,
    session: str,
    stress: float,
    direction: str,
    memory: HierarchicalMemory,
    k: int = 5,
    caution_threshold_r: float = -0.3,
    boost_threshold_r: float = 0.5,
) -> EnrichedContext:
    """Pre-trade enrichment: retrieve K similar episodes, summarize,
    flag cautions (analog losers) and boosts (analog winners).

    Output is consumed by:
      - Firm-board Auditor role (uses ``cautions`` to oppose entry)
      - JARVIS narrative log (uses ``summary`` for audit trail)
      - Decision-journal extras (records ``avg_analog_r`` for
        post-trade calibration check)
    """
    retrieved = retrieve_similar(
        query_text=current_narrative,
        regime=regime,
        session=session,
        stress=stress,
        direction=direction,
        memory=memory,
        k=k,
    )
    if not retrieved:
        return EnrichedContext(
            n_retrieved=0,
            summary="No analog episodes available; retrieval skipped.",
        )
    eps = [r.episode for r in retrieved]
    summary = summarize_episodes(eps)
    avg_r = sum(e.realized_r for e in eps) / len(eps)
    n_pos = sum(1 for e in eps if e.realized_r > 0)

    cautions: list[str] = []
    boosts: list[str] = []
    if avg_r <= caution_threshold_r:
        cautions.append(
            f"top-{len(eps)} analogs averaged {avg_r:+.2f}R (below {caution_threshold_r:+.2f}R caution threshold)",
        )
    if avg_r >= boost_threshold_r:
        boosts.append(
            f"top-{len(eps)} analogs averaged {avg_r:+.2f}R (above {boost_threshold_r:+.2f}R boost threshold)",
        )
    # Per-episode flags for the worst losers
    for r in retrieved:
        if r.episode.realized_r <= -1.0:
            cautions.append(
                f"analog (rank {r.rank}, score {r.score:.2f}) "
                f"lost {r.episode.realized_r:+.2f}R: "
                f'"{r.episode.narrative[:60]}"',
            )

    return EnrichedContext(
        n_retrieved=len(retrieved),
        summary=summary,
        cautions=cautions,
        boosts=boosts,
        avg_analog_r=round(avg_r, 4),
        win_rate_analogs=round(n_pos / len(eps), 3),
        retrieved=retrieved,
    )


# ─── Importance-scored forgetting ─────────────────────────────────


def evict_redundant_episodes(
    memory: HierarchicalMemory,
    *,
    target_size: int,
) -> int:
    """When the journal grows past ``target_size``, evict the most
    REDUNDANT episodes (highest similarity to many others; they
    contribute least new information). Pure ranking, no actual delete
    -- caller can act on the returned list of indices to forget.

    Returns the number of episodes eligible for eviction. Mutates
    ``memory._episodes`` in place if and only if there's overflow."""
    n = len(memory._episodes)
    if n <= target_size:
        return 0
    # Compute redundancy = sum of pairwise text similarities to all others
    embeds = [hash_embed(e.narrative) for e in memory._episodes]
    redundancy: list[float] = []
    for i in range(n):
        s = sum(cosine_sparse(embeds[i], embeds[j]) for j in range(n) if j != i)
        redundancy.append(s)

    # Drop the most-redundant first
    keep_n = target_size
    paired = sorted(
        zip(redundancy, range(n), strict=True),
        key=lambda t: t[0],
    )
    keep_indices = sorted(idx for _, idx in paired[:keep_n])
    new_episodes = [memory._episodes[i] for i in keep_indices]
    n_evicted = n - len(new_episodes)
    memory._episodes = new_episodes
    return n_evicted
