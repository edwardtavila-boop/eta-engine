"""
EVOLUTIONARY TRADING ALGO  //  brain.jarvis_journals
==========================================
Three append-only JSONL journals: state, recommendations, anomalies.

Why this exists
---------------
Every JARVIS observation produces three streams:
  1. SessionStateSnapshot — what JARVIS saw (state journal)
  2. Recommendations      — what JARVIS suggested (rec journal)
  3. Anomalies            — what JARVIS flagged (anomaly journal)

All three are append-only JSONL with timestamp + payload. Failures
during write are silent (observability, not load-bearing). Read
failures raise. The journals enable post-hoc audit ("what did JARVIS
know / suggest / flag at this ts?") and operator review.

Public API
----------
  * ``StateJournal``         — SessionStateSnapshot writer/reader
  * ``RecommendationJournal``— list[Recommendation] writer/reader
  * ``AnomalyJournal``       — list[Anomaly] writer/reader
  * ``replay_for_decision()``— legacy compat: state journal lookup by ts
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_anomaly import Anomaly
    from eta_engine.brain.jarvis_recommender import Recommendation
    from eta_engine.brain.jarvis_session_state import SessionStateSnapshot


_REPO_ROOT = Path(__file__).resolve().parents[2]
_JOURNAL_DIR = _REPO_ROOT / "eta_engine" / "docs" / "journals"

_DEFAULT_STATE = _JOURNAL_DIR / "jarvis_state.jsonl"
_DEFAULT_RECS = _JOURNAL_DIR / "jarvis_recommendations.jsonl"
_DEFAULT_ANOMALIES = _JOURNAL_DIR / "jarvis_anomalies.jsonl"


class _BaseJournal:
    """Generic append-only JSONL writer + reader."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def _append(self, entry: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass

    def read_all(self) -> list[dict]:
        if not self._path.exists():
            return []
        out: list[dict] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def latest(self) -> dict | None:
        entries = self.read_all()
        return entries[-1] if entries else None


class StateJournal(_BaseJournal):
    """Append-only journal of SessionStateSnapshot."""

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(path or _DEFAULT_STATE)

    def append(self, snap: SessionStateSnapshot) -> None:
        self._append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "snapshot": snap.model_dump(mode="json"),
            }
        )

    def replay_for_decision(self, ts: datetime) -> dict | None:
        """Return the entry active at `ts` (last entry with ts <= cutoff)."""
        entries = self.read_all()
        if not entries:
            return None
        cutoff = ts.isoformat()
        active: dict | None = None
        for entry in entries:
            if entry.get("ts", "") <= cutoff:
                active = entry
            else:
                break
        return active


class RecommendationJournal(_BaseJournal):
    """Append-only journal of recommendation lists."""

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(path or _DEFAULT_RECS)

    def append(self, recommendations: list[Recommendation]) -> None:
        self._append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "n": len(recommendations),
                "recommendations": [r.model_dump(mode="json") for r in recommendations],
            }
        )


class AnomalyJournal(_BaseJournal):
    """Append-only journal of detected anomalies."""

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(path or _DEFAULT_ANOMALIES)

    def append(self, anomalies: list[Anomaly]) -> None:
        if not anomalies:
            return  # don't bloat the journal with empty entries
        self._append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "n": len(anomalies),
                "anomalies": [a.model_dump(mode="json") for a in anomalies],
            }
        )


# Backward-compat alias
JarvisStateJournal = StateJournal


def replay_for_decision(ts: datetime, *, journal: StateJournal | None = None) -> dict | None:
    """Legacy compat function — use `StateJournal.replay_for_decision`."""
    j = journal or StateJournal()
    return j.replay_for_decision(ts)
