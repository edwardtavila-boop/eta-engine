"""Hierarchical memory for JARVIS (Wave-8 #3, 2026-04-27).

Three-tier memory inspired by cognitive architectures:

  * EPISODIC   -- "what happened" -- per-trade detail (entry, exit,
                   regime, narrative, realized R)
  * SEMANTIC   -- "what is true in general" -- aggregated patterns
                   ("in regime X with signal Y, win rate is Z")
  * PROCEDURAL -- "how I do things" -- versioned hyperparameter sets
                   with lineage (which mutation produced which)

Lean implementation: pure stdlib, JSONL persistence, vector retrieval
via cosine similarity on hand-rolled feature vectors. No FAISS, no
Chroma, no embedding models -- those are obvious upgrade points but
not needed to start compounding on past experience.

Use case (the supercharge play):

    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

    mem = HierarchicalMemory.default()

    # On every trade close:
    mem.record_episode(
        signal_id="cascade_hunter_2026-04-27T15:32",
        regime="bullish_low_vol",
        session="rth",
        stress=0.3,
        direction="long",
        realized_r=2.4,
        narrative="EMA stack aligned, sage approved, sentiment +0.4",
    )

    # Before committing the next trade:
    similar = mem.recall_similar(
        regime="bullish_low_vol", session="rth", stress=0.3, k=5,
    )
    avg_r = sum(e.realized_r for e in similar) / max(len(similar), 1)
    if avg_r < 0:
        # Past similar setups lost -- defer or shrink size

The key shift this enables: JARVIS no longer reasons only from the
current snapshot. It explicitly retrieves analogous historical
situations and conditions on what HAPPENED in those situations.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
EPISODIC_PATH = ROOT / "state" / "memory" / "episodic.jsonl"
SEMANTIC_PATH = ROOT / "state" / "memory" / "semantic.json"
PROCEDURAL_PATH = ROOT / "state" / "memory" / "procedural.jsonl"


@dataclass
class Episode:
    """A single completed trade with rich context."""

    ts: str
    signal_id: str
    regime: str
    session: str
    stress: float
    direction: Literal["long", "short"]
    realized_r: float
    narrative: str = ""
    extra: dict = field(default_factory=dict)

    def feature_vector(self) -> list[float]:
        """Compact numeric signature for similarity lookup.

        Chosen to be hash-stable and dimension-fixed so we can cosine-
        compare across episodes without a learned embedding.
        """
        return [
            self._regime_bucket(),
            self._session_bucket(),
            float(self.stress),
            1.0 if self.direction == "long" else -1.0,
        ]

    def _regime_bucket(self) -> float:
        # Coarse 4-bucket mapping from common regime labels to floats
        # in [0, 1]. Unknown labels land at 0.5 (neutral).
        table = {
            "bearish_high_vol": 0.0,
            "bearish_low_vol": 0.25,
            "neutral": 0.5,
            "bullish_low_vol": 0.75,
            "bullish_high_vol": 1.0,
        }
        return float(table.get(self.regime.lower(), 0.5))

    def _session_bucket(self) -> float:
        table = {
            "rth": 1.0,
            "premarket": 0.6,
            "afterhours": 0.4,
            "overnight": 0.0,
        }
        return float(table.get(self.session.lower(), 0.5))


@dataclass
class SemanticFact:
    """Aggregated pattern across many episodes."""

    pattern: str           # e.g. "bullish_low_vol+rth+long"
    n_episodes: int
    win_rate: float
    avg_r: float
    last_updated: str


@dataclass
class ProceduralVersion:
    """One historical hyperparameter set with provenance."""

    version_id: str
    parent_id: str | None
    created_at: str
    params: dict
    realized_metric: float | None = None  # e.g. avg-R after rollout
    notes: str = ""


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b, strict=True))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(x * x for x in b))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


class HierarchicalMemory:
    """Episodic + semantic + procedural memory with persistent backing.

    Thread-safe; all writes hit JSONL/JSON immediately so a crash
    doesn't lose the last few hours of accumulated experience.
    """

    def __init__(
        self,
        *,
        episodic_path: Path = EPISODIC_PATH,
        semantic_path: Path = SEMANTIC_PATH,
        procedural_path: Path = PROCEDURAL_PATH,
    ) -> None:
        self.episodic_path = episodic_path
        self.semantic_path = semantic_path
        self.procedural_path = procedural_path
        self._lock = threading.Lock()
        self._episodes: list[Episode] = []
        self._semantic: dict[str, SemanticFact] = {}
        self._procedural: list[ProceduralVersion] = []
        self._load()

    @classmethod
    def default(cls) -> HierarchicalMemory:
        return cls()

    # ── Episodic ────────────────────────────────────────────────

    def record_episode(
        self,
        *,
        signal_id: str,
        regime: str,
        session: str,
        stress: float,
        direction: Literal["long", "short"],
        realized_r: float,
        narrative: str = "",
        extra: dict | None = None,
    ) -> Episode:
        ep = Episode(
            ts=datetime.now(UTC).isoformat(),
            signal_id=signal_id,
            regime=regime,
            session=session,
            stress=float(stress),
            direction=direction,
            realized_r=float(realized_r),
            narrative=narrative,
            extra=extra or {},
        )
        with self._lock:
            self._episodes.append(ep)
            self._append_episodic(ep)
            self._update_semantic_for(ep)
        return ep

    def recall_similar(
        self,
        *,
        regime: str,
        session: str,
        stress: float,
        direction: Literal["long", "short"] = "long",
        k: int = 5,
    ) -> list[Episode]:
        """Return the k most similar episodes by cosine similarity on
        the feature vector. Empty list if no episodes yet."""
        probe = Episode(
            ts="", signal_id="probe", regime=regime, session=session,
            stress=stress, direction=direction, realized_r=0.0,
        )
        probe_vec = probe.feature_vector()
        with self._lock:
            scored = [
                (_cosine(probe_vec, e.feature_vector()), e)
                for e in self._episodes
            ]
        scored.sort(key=lambda t: t[0], reverse=True)
        return [e for _, e in scored[:k]]

    # ── Semantic ────────────────────────────────────────────────

    def _semantic_key(self, ep: Episode) -> str:
        return f"{ep.regime}+{ep.session}+{ep.direction}"

    def _update_semantic_for(self, ep: Episode) -> None:
        key = self._semantic_key(ep)
        prior = self._semantic.get(key)
        if prior is None:
            new_fact = SemanticFact(
                pattern=key,
                n_episodes=1,
                win_rate=1.0 if ep.realized_r > 0 else 0.0,
                avg_r=float(ep.realized_r),
                last_updated=datetime.now(UTC).isoformat(),
            )
        else:
            n = prior.n_episodes + 1
            wins_prior = prior.win_rate * prior.n_episodes
            wins_new = wins_prior + (1 if ep.realized_r > 0 else 0)
            avg_r = (prior.avg_r * prior.n_episodes + ep.realized_r) / n
            new_fact = SemanticFact(
                pattern=key,
                n_episodes=n,
                win_rate=wins_new / n,
                avg_r=avg_r,
                last_updated=datetime.now(UTC).isoformat(),
            )
        self._semantic[key] = new_fact
        self._save_semantic()

    def lookup_pattern(
        self,
        *,
        regime: str,
        session: str,
        direction: Literal["long", "short"],
    ) -> SemanticFact | None:
        with self._lock:
            return self._semantic.get(f"{regime}+{session}+{direction}")

    # ── Procedural ──────────────────────────────────────────────

    def record_procedural_version(
        self,
        *,
        version_id: str,
        parent_id: str | None,
        params: dict,
        realized_metric: float | None = None,
        notes: str = "",
    ) -> ProceduralVersion:
        v = ProceduralVersion(
            version_id=version_id,
            parent_id=parent_id,
            created_at=datetime.now(UTC).isoformat(),
            params=dict(params),
            realized_metric=realized_metric,
            notes=notes,
        )
        with self._lock:
            self._procedural.append(v)
            self._append_procedural(v)
        return v

    def best_procedural_version(self) -> ProceduralVersion | None:
        with self._lock:
            scored = [v for v in self._procedural if v.realized_metric is not None]
        if not scored:
            return None
        return max(scored, key=lambda v: v.realized_metric or float("-inf"))

    def procedural_lineage(self, version_id: str) -> list[ProceduralVersion]:
        """Walk parent_id pointers back to the root."""
        with self._lock:
            by_id = {v.version_id: v for v in self._procedural}
        chain: list[ProceduralVersion] = []
        cur = by_id.get(version_id)
        while cur is not None:
            chain.append(cur)
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        return chain

    # ── Persistence ─────────────────────────────────────────────

    def _load(self) -> None:
        self._load_episodic()
        self._load_semantic()
        self._load_procedural()

    def _append_episodic(self, ep: Episode) -> None:
        try:
            self.episodic_path.parent.mkdir(parents=True, exist_ok=True)
            with self.episodic_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(ep)) + "\n")
        except OSError as exc:
            logger.warning("memory: episodic append failed (%s)", exc)

    def _load_episodic(self) -> None:
        if not self.episodic_path.exists():
            return
        try:
            for line in self.episodic_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                self._episodes.append(Episode(**d))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("memory: episodic load failed (%s); fresh start", exc)

    def _save_semantic(self) -> None:
        try:
            self.semantic_path.parent.mkdir(parents=True, exist_ok=True)
            self.semantic_path.write_text(
                json.dumps(
                    {k: asdict(v) for k, v in self._semantic.items()}, indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("memory: semantic save failed (%s)", exc)

    def _load_semantic(self) -> None:
        if not self.semantic_path.exists():
            return
        try:
            data = json.loads(self.semantic_path.read_text(encoding="utf-8"))
            for k, v in data.items():
                self._semantic[k] = SemanticFact(**v)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("memory: semantic load failed (%s); fresh start", exc)

    def _append_procedural(self, v: ProceduralVersion) -> None:
        try:
            self.procedural_path.parent.mkdir(parents=True, exist_ok=True)
            with self.procedural_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(v)) + "\n")
        except OSError as exc:
            logger.warning("memory: procedural append failed (%s)", exc)

    def _load_procedural(self) -> None:
        if not self.procedural_path.exists():
            return
        try:
            for line in self.procedural_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                self._procedural.append(ProceduralVersion(**d))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("memory: procedural load failed (%s); fresh start", exc)
