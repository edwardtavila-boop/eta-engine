"""
EVOLUTIONARY TRADING ALGO  //  brain.rationale_miner
========================================
Cluster trade rationales, correlate clusters with grade outcomes.

Why this exists
---------------
Every trade gets a one-line rationale recorded in the decision journal:
"fade open gap up / RANGING regime / confluence 7". After 500 trades,
some phrases win 80% of the time and some win 20%. The miner surfaces
those buckets -- WITHOUT any ML library. Token-bag clustering + expectancy
is enough to answer "which of my habitual setups actually pays."

Design
------
* Stdlib only. No sklearn, no numpy heavy use.
* Tokenize by stripping punctuation + lowercasing + splitting on whitespace.
* Stop-words removed (generic verbs, pronouns).
* Group trades by shared n-grams of length 1 AND 2 that appear at least
  ``min_cluster_size`` times across the batch.
* For each cluster compute: count, win_rate, mean_r, mean_grade,
  expectancy (mean_r), sharpe-ish (mean_r / stdev_r).

Public API
----------
  * ``RationaleRecord`` -- one trade's rationale + outcome
  * ``Cluster`` -- mined cluster with stats
  * ``MiningReport`` -- full output
  * ``RationaleMiner`` -- tokenize + cluster + score
  * ``tokenize_rationale`` -- exposed for testing
"""

from __future__ import annotations

import math
import re
from collections import Counter

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Token utilities
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "to",
        "of",
        "on",
        "in",
        "at",
        "by",
        "for",
        "with",
        "from",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "i",
        "we",
        "you",
        "me",
        "my",
        "your",
        "our",
        "and",
        "or",
        "but",
        "not",
        "no",
        "yes",
        "did",
        "do",
        "does",
        "had",
        "has",
        "have",
        "just",
        "then",
        "than",
        "so",
        "as",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z]+")


def tokenize_rationale(rationale: str) -> list[str]:
    """Lowercase, strip punctuation, drop stopwords. Returns a list of tokens."""
    if not rationale:
        return []
    tokens = [t.lower() for t in _TOKEN_RE.findall(rationale)]
    return [t for t in tokens if t not in _STOPWORDS and len(t) >= 2]


def ngrams(tokens: list[str], *, n: int) -> list[str]:
    """Space-joined contiguous n-grams."""
    if n <= 0 or len(tokens) < n:
        return []
    return [" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class RationaleRecord(BaseModel):
    """One trade's rationale tied to outcome R and grade."""

    trade_id: str = Field(min_length=1)
    rationale: str = Field(default="")
    r_captured: float
    grade_total: float | None = Field(default=None, ge=0.0, le=100.0)


class Cluster(BaseModel):
    """One phrase-bucket and its outcome stats."""

    phrase: str = Field(min_length=1)
    n: int = Field(ge=1)
    trade_ids: list[str]
    win_rate: float = Field(ge=0.0, le=1.0)
    mean_r: float
    median_r: float
    std_r: float = Field(ge=0.0)
    mean_grade: float | None = Field(default=None)
    expectancy: float
    sharpe: float = Field(
        description="mean_r / std_r; 0 when std_r == 0",
    )


class MiningReport(BaseModel):
    n_records: int
    clusters: list[Cluster]
    top_winners: list[Cluster]
    top_losers: list[Cluster]
    uncategorized: list[str] = Field(
        default_factory=list,
        description="trade_ids that matched no cluster",
    )


# ---------------------------------------------------------------------------
# Miner
# ---------------------------------------------------------------------------


class RationaleMiner:
    """Cluster rationales, score by expectancy.

    Parameters
    ----------
    min_cluster_size: minimum trades that must share a phrase.
    max_ngram: 1 = unigrams only, 2 = uni + bi, etc.
    """

    def __init__(
        self,
        *,
        min_cluster_size: int = 3,
        max_ngram: int = 2,
    ) -> None:
        if min_cluster_size < 1:
            raise ValueError("min_cluster_size must be >= 1")
        if max_ngram not in (1, 2, 3):
            raise ValueError("max_ngram must be 1, 2, or 3")
        self.min_cluster_size = min_cluster_size
        self.max_ngram = max_ngram

    # --- top-level --------------------------------------------------------

    def mine(self, records: list[RationaleRecord]) -> MiningReport:
        if not records:
            return MiningReport(n_records=0, clusters=[], top_winners=[], top_losers=[])

        phrase_to_trades = self._build_inverted_index(records)
        record_by_id = {r.trade_id: r for r in records}

        clusters: list[Cluster] = []
        for phrase, trade_ids in phrase_to_trades.items():
            if len(trade_ids) < self.min_cluster_size:
                continue
            bucket = [record_by_id[tid] for tid in trade_ids]
            clusters.append(self._score_cluster(phrase, bucket))

        clusters.sort(key=lambda c: c.expectancy, reverse=True)

        clustered_ids: set[str] = set()
        for c in clusters:
            clustered_ids.update(c.trade_ids)
        uncategorized = [r.trade_id for r in records if r.trade_id not in clustered_ids]

        top_winners = [c for c in clusters if c.mean_r > 0][:5]
        top_losers = [c for c in clusters if c.mean_r < 0][-5:][::-1]

        return MiningReport(
            n_records=len(records),
            clusters=clusters,
            top_winners=top_winners,
            top_losers=top_losers,
            uncategorized=uncategorized,
        )

    # --- internals --------------------------------------------------------

    def _build_inverted_index(
        self,
        records: list[RationaleRecord],
    ) -> dict[str, list[str]]:
        """For each phrase, list the trade_ids where it occurs."""
        out: dict[str, list[str]] = {}
        for rec in records:
            tokens = tokenize_rationale(rec.rationale)
            phrases: set[str] = set(tokens)  # unigrams
            if self.max_ngram >= 2:
                phrases.update(ngrams(tokens, n=2))
            if self.max_ngram >= 3:
                phrases.update(ngrams(tokens, n=3))
            for p in phrases:
                out.setdefault(p, []).append(rec.trade_id)
        return out

    def _score_cluster(
        self,
        phrase: str,
        records: list[RationaleRecord],
    ) -> Cluster:
        n = len(records)
        rs = [r.r_captured for r in records]
        wins = sum(1 for r in rs if r > 0)
        win_rate = wins / n
        mean_r = sum(rs) / n
        sorted_rs = sorted(rs)
        mid = n // 2
        median_r = sorted_rs[mid] if n % 2 == 1 else 0.5 * (sorted_rs[mid - 1] + sorted_rs[mid])

        if n > 1:
            var = sum((r - mean_r) ** 2 for r in rs) / (n - 1)
            std_r = math.sqrt(var)
        else:
            std_r = 0.0

        graded = [r for r in records if r.grade_total is not None]
        mean_grade = (
            sum(r.grade_total for r in graded) / len(graded)  # type: ignore[misc]
            if graded
            else None
        )

        sharpe = mean_r / std_r if std_r > 0 else 0.0

        return Cluster(
            phrase=phrase,
            n=n,
            trade_ids=[r.trade_id for r in records],
            win_rate=round(win_rate, 4),
            mean_r=round(mean_r, 4),
            median_r=round(median_r, 4),
            std_r=round(std_r, 4),
            mean_grade=round(mean_grade, 2) if mean_grade is not None else None,
            expectancy=round(mean_r, 4),
            sharpe=round(sharpe, 4),
        )


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def winners_minus_losers(report: MiningReport) -> list[tuple[str, float]]:
    """Return (phrase, expectancy) sorted desc. Handy for quick printout."""
    ranked = sorted(
        report.clusters,
        key=lambda c: c.expectancy,
        reverse=True,
    )
    return [(c.phrase, c.expectancy) for c in ranked]


def coverage(report: MiningReport) -> float:
    """Fraction of input records that landed in at least one cluster."""
    if report.n_records == 0:
        return 0.0
    uncov = len(report.uncategorized)
    return round(1.0 - uncov / report.n_records, 4)


def phrase_frequency(records: list[RationaleRecord], *, top_k: int = 10, max_ngram: int = 2) -> list[tuple[str, int]]:
    """Most common phrases across the batch. Useful for sanity-checking
    tokenization and deciding what min_cluster_size should be.
    """
    counter: Counter[str] = Counter()
    for r in records:
        tokens = tokenize_rationale(r.rationale)
        phrases = set(tokens)
        if max_ngram >= 2:
            phrases.update(ngrams(tokens, n=2))
        if max_ngram >= 3:
            phrases.update(ngrams(tokens, n=3))
        counter.update(phrases)
    return counter.most_common(top_k)
