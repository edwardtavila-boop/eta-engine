"""Operator-facing JARVIS query interface (Wave-12, 2026-04-27).

When JARVIS is the source of truth, the operator needs read-only
queries to interrogate that truth: "what has JARVIS been deciding
in the last hour?", "where does it disagree with itself most?",
"what regime dominates its journaled experience?".

This module is the read layer: structured, deterministic, no side
effects. Suitable for CLI / dashboard / Slack-bot consumers.

Public API
----------

  * recent_verdicts(n_hours=24)      -- last N hours of consolidated
                                          verdicts
  * verdict_breakdown_by_action()    -- counts per APPROVED/DENIED/etc.
  * regime_distribution(n_hours=24)  -- regime histogram from verdicts
  * disagreement_hotspots()           -- pairs (subsystem, action) with
                                          the lowest avg consensus
  * memory_regime_stats()             -- per-regime episode count and
                                          win-rate from hierarchical
                                          memory
  * top_analog_episodes(narrative)    -- RAG retrieval as a query

All functions return plain dicts/lists for easy JSON-serialization.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.scripts import workspace_roots

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

logger = logging.getLogger(__name__)

DEFAULT_VERDICT_LOG = workspace_roots.ETA_JARVIS_VERDICTS_PATH
DEFAULT_TRADE_LOG = workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH


# ─── Helpers ─────────────────────────────────────────────────────


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError as exc:
        logger.warning("admin_query: read %s failed (%s)", path, exc)
    return out


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (TypeError, ValueError):
        return None


def _within_window(record_ts: str | None, since: datetime | None) -> bool:
    if since is None:
        return True
    dt = _parse_ts(record_ts)
    if dt is None:
        return False
    return dt >= since


# ─── Public queries ──────────────────────────────────────────────


@dataclass
class RecentVerdictsReport:
    """Summary of recent JARVIS consolidated verdicts."""

    window_hours: float
    n_total: int
    by_final_verdict: dict[str, int] = field(default_factory=dict)
    by_subsystem: dict[str, int] = field(default_factory=dict)
    avg_confidence: float = 0.0
    n_with_cautions: int = 0
    n_with_dissent: int = 0


def recent_verdicts(
    *,
    n_hours: float = 24,
    log_path: Path = DEFAULT_VERDICT_LOG,
) -> RecentVerdictsReport:
    """Aggregate JARVIS intelligence verdicts over the last n hours."""
    cutoff = datetime.now(UTC) - timedelta(hours=n_hours)
    records = [r for r in _read_jsonl(log_path) if _within_window(r.get("ts"), cutoff)]
    if not records:
        return RecentVerdictsReport(window_hours=n_hours, n_total=0)
    by_verdict: Counter[str] = Counter()
    by_subsystem: Counter[str] = Counter()
    conf_sum = 0.0
    n_cautions = 0
    n_dissent = 0
    for r in records:
        by_verdict[str(r.get("final_verdict", "UNKNOWN"))] += 1
        by_subsystem[str(r.get("subsystem", "UNKNOWN"))] += 1
        conf_sum += float(r.get("confidence", 0.0))
        if r.get("rag_cautions"):
            n_cautions += 1
        if r.get("firm_board_consensus", 1.0) < 0.6:
            n_dissent += 1
    return RecentVerdictsReport(
        window_hours=n_hours,
        n_total=len(records),
        by_final_verdict=dict(by_verdict),
        by_subsystem=dict(by_subsystem),
        avg_confidence=round(conf_sum / len(records), 3),
        n_with_cautions=n_cautions,
        n_with_dissent=n_dissent,
    )


def verdict_breakdown_by_action(
    *,
    n_hours: float = 168,  # 7 days
    log_path: Path = DEFAULT_VERDICT_LOG,
) -> dict[str, dict[str, int]]:
    """Pivot table: action -> verdict -> count."""
    cutoff = datetime.now(UTC) - timedelta(hours=n_hours)
    records = [r for r in _read_jsonl(log_path) if _within_window(r.get("ts"), cutoff)]
    out: dict[str, dict[str, int]] = {}
    for r in records:
        action = str(r.get("action", "unknown"))
        verdict = str(r.get("final_verdict", "UNKNOWN"))
        if action not in out:
            out[action] = {}
        out[action][verdict] = out[action].get(verdict, 0) + 1
    return out


def regime_distribution(
    *,
    n_hours: float = 24,
    memory: HierarchicalMemory | None = None,
) -> dict[str, int]:
    """Per-regime episode count from hierarchical memory.

    If ``memory`` is None we lazily construct the default. Filters
    to episodes within the last n_hours when ts parses successfully.
    """
    if memory is None:
        from eta_engine.brain.jarvis_v3.memory_hierarchy import (
            HierarchicalMemory,
        )

        memory = HierarchicalMemory()
    cutoff = datetime.now(UTC) - timedelta(hours=n_hours)
    counter: Counter[str] = Counter()
    for ep in memory._episodes:
        if not _within_window(getattr(ep, "ts", ""), cutoff):
            continue
        counter[ep.regime] += 1
    return dict(counter)


def disagreement_hotspots(
    *,
    n_hours: float = 168,
    min_count: int = 3,
    log_path: Path = DEFAULT_VERDICT_LOG,
) -> list[dict]:
    """Find (subsystem, action) pairs where firm-board consensus has
    been LOWEST -- the spots where the roles routinely disagree.

    Returns a sorted list of dicts with avg_consensus, count, and
    label. Useful for routing operator attention to setups where
    JARVIS is uncertain."""
    cutoff = datetime.now(UTC) - timedelta(hours=n_hours)
    records = [r for r in _read_jsonl(log_path) if _within_window(r.get("ts"), cutoff)]
    grouped: dict[tuple[str, str], list[float]] = {}
    for r in records:
        key = (
            str(r.get("subsystem", "")),
            str(r.get("action", "")),
        )
        grouped.setdefault(key, []).append(
            float(r.get("firm_board_consensus", 1.0)),
        )
    out: list[dict] = []
    for (sub, act), consensus_list in grouped.items():
        if len(consensus_list) < min_count:
            continue
        avg = sum(consensus_list) / len(consensus_list)
        out.append(
            {
                "subsystem": sub,
                "action": act,
                "avg_consensus": round(avg, 3),
                "count": len(consensus_list),
            }
        )
    return sorted(out, key=lambda d: d["avg_consensus"])


@dataclass
class MemoryRegimeStats:
    regime: str
    n_episodes: int
    win_rate: float
    avg_r: float


def memory_regime_stats(
    memory: HierarchicalMemory | None = None,
) -> list[MemoryRegimeStats]:
    """Per-regime win-rate + avg-R from journaled episodes."""
    if memory is None:
        from eta_engine.brain.jarvis_v3.memory_hierarchy import (
            HierarchicalMemory,
        )

        memory = HierarchicalMemory()
    grouped: dict[str, list[float]] = {}
    for ep in memory._episodes:
        grouped.setdefault(ep.regime, []).append(ep.realized_r)
    out: list[MemoryRegimeStats] = []
    for regime, rs in grouped.items():
        n = len(rs)
        wr = sum(1 for r in rs if r > 0) / max(n, 1)
        avg = sum(rs) / max(n, 1)
        out.append(
            MemoryRegimeStats(
                regime=regime,
                n_episodes=n,
                win_rate=round(wr, 3),
                avg_r=round(avg, 4),
            )
        )
    return sorted(out, key=lambda s: s.n_episodes, reverse=True)


def second_brain_snapshot(
    memory: HierarchicalMemory | None = None,
    *,
    top_n: int = 5,
) -> dict:
    """Return Jarvis's hierarchical-memory status as plain JSON."""
    if memory is None:
        from eta_engine.brain.jarvis_v3.memory_hierarchy import (
            HierarchicalMemory,
        )

        memory = HierarchicalMemory()
    try:
        return memory.snapshot(top_n=top_n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("admin_query: second brain snapshot failed (%s)", exc)
        return {
            "status": "unavailable",
            "error": str(exc),
            "n_episodes": 0,
            "semantic_patterns": 0,
            "procedural_versions": 0,
        }


def second_brain_playbook(
    memory: HierarchicalMemory | None = None,
    *,
    min_episodes: int = 30,
    top_n: int = 5,
) -> dict:
    """Return best/worst historical setup patterns from Jarvis memory."""
    if memory is None:
        from eta_engine.brain.jarvis_v3.memory_hierarchy import (
            HierarchicalMemory,
        )

        memory = HierarchicalMemory()
    try:
        return memory.semantic_playbook(min_episodes=min_episodes, top_n=top_n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("admin_query: second brain playbook failed (%s)", exc)
        return {
            "eligible_patterns": 0,
            "best_patterns": [],
            "worst_patterns": [],
            "favor_patterns": [],
            "avoid_patterns": [],
            "error": str(exc),
        }


def top_analog_episodes(
    *,
    narrative: str,
    regime: str,
    session: str,
    stress: float,
    direction: str = "long",
    k: int = 5,
    memory: HierarchicalMemory | None = None,
) -> list[dict]:
    """RAG retrieval exposed as a query. Returns the top-k similar
    episodes as plain dicts."""
    if memory is None:
        from eta_engine.brain.jarvis_v3.memory_hierarchy import (
            HierarchicalMemory,
        )

        memory = HierarchicalMemory()
    try:
        from eta_engine.brain.jarvis_v3.memory_rag import retrieve_similar

        retrieved = retrieve_similar(
            query_text=narrative,
            regime=regime,
            session=session,
            stress=stress,
            direction=direction,
            memory=memory,
            k=k,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("admin_query: rag retrieve failed (%s)", exc)
        return []
    return [
        {
            "rank": r.rank,
            "score": r.score,
            "signal_id": r.episode.signal_id,
            "ts": r.episode.ts,
            "regime": r.episode.regime,
            "realized_r": r.episode.realized_r,
            "narrative": r.episode.narrative[:120],
        }
        for r in retrieved
    ]


def trade_close_stats(
    *,
    n_hours: float = 24,
    log_path: Path = DEFAULT_TRADE_LOG,
) -> dict:
    """Aggregate stats from the trade-close feedback log."""
    cutoff = datetime.now(UTC) - timedelta(hours=n_hours)
    records = [r for r in _read_jsonl(log_path) if _within_window(r.get("ts"), cutoff)]
    if not records:
        return {"n": 0, "avg_r": 0.0, "win_rate": 0.0}
    rs = [float(r.get("realized_r", 0.0)) for r in records]
    wins = sum(1 for r in rs if r > 0)
    return {
        "n": len(rs),
        "avg_r": round(sum(rs) / len(rs), 4),
        "win_rate": round(wins / len(rs), 3),
        "best_r": round(max(rs), 4),
        "worst_r": round(min(rs), 4),
    }
