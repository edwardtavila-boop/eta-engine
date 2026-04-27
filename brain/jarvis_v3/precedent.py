"""
JARVIS v3 // precedent
======================
Precedent knowledge graph.

v2 memory is a ``deque[JarvisContext]`` -- a linear ring buffer. v3 upgrades
to a keyed store where entries are indexed by their ``(regime,
session_phase, event_category, binding_constraint)`` tuple. When a new
context comes in, we can ask:

  "Last N times we were in {CRISIS, OPEN_DRIVE, FOMC_IMMINENT, macro_event},
   what action did we take? What was the outcome? What was the median R?"

The shape is an adjacency-list style dict keyed by the tuple, with a
bounded list of outcomes per bucket. Lookups are O(1). Persistence is
JSON with a bounded per-bucket tail.

Pure stdlib + pydantic.
"""

from __future__ import annotations

import json
import statistics
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class PrecedentKey(BaseModel):
    """Composite key that defines a precedent bucket."""

    model_config = ConfigDict(frozen=True)

    regime: str = Field(min_length=1)
    session_phase: str = Field(min_length=1)
    event_category: str = Field(default="none")
    binding_constraint: str = Field(default="none")

    @property
    def tuple_key(self) -> tuple[str, str, str, str]:
        return (
            self.regime.upper(),
            self.session_phase.upper(),
            self.event_category.lower(),
            self.binding_constraint.lower(),
        )


class PrecedentEntry(BaseModel):
    """One historical outcome in a precedent bucket."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    action: str = Field(min_length=1)  # TRADE / REDUCE / STAND_ASIDE / ...
    verdict: str = Field(default="NA")  # APPROVED / DENIED / ...
    outcome_correct: int | None = None
    realized_r: float | None = None
    note: str = ""


class PrecedentQuery(BaseModel):
    """Summary stats pulled from a bucket on query."""

    model_config = ConfigDict(frozen=True)

    key: PrecedentKey
    n: int = Field(ge=0)
    win_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    median_r: float | None = None
    mean_r: float | None = None
    last_action: str | None = None
    last_ts: datetime | None = None
    actions_freq: dict[str, int] = Field(default_factory=dict)
    suggestion: str = ""


class PrecedentGraph:
    """Bucketed precedent store with bounded per-bucket history.

    Parameters
    ----------
    max_per_bucket : cap on history retained per key.
    """

    def __init__(self, max_per_bucket: int = 200) -> None:
        self._store: dict[tuple[str, str, str, str], deque[PrecedentEntry]] = {}
        self.max_per_bucket = max_per_bucket

    def record(self, key: PrecedentKey, entry: PrecedentEntry) -> None:
        k = key.tuple_key
        if k not in self._store:
            self._store[k] = deque(maxlen=self.max_per_bucket)
        self._store[k].append(entry)

    def query(self, key: PrecedentKey) -> PrecedentQuery:
        k = key.tuple_key
        entries = list(self._store.get(k, []))
        n = len(entries)
        if n == 0:
            return PrecedentQuery(
                key=key,
                n=0,
                suggestion="no precedent -- proceed with baseline policy",
            )
        labeled = [e for e in entries if e.outcome_correct is not None]
        wr = sum(e.outcome_correct for e in labeled if e.outcome_correct) / len(labeled) if labeled else None
        rs = [e.realized_r for e in entries if e.realized_r is not None]
        median_r = statistics.median(rs) if rs else None
        mean_r = statistics.fmean(rs) if rs else None
        last = entries[-1]
        freq: dict[str, int] = {}
        for e in entries:
            freq[e.action] = freq.get(e.action, 0) + 1
        # Suggestion: if strong positive prior, bias to TRADE; if negative, STAND_ASIDE.
        if mean_r is not None and mean_r > 0.5 and (wr or 0) >= 0.55:
            sugg = f"precedent favors TRADE (wr={wr or 0:.0%}, mean_r={mean_r:+.2f})"
        elif mean_r is not None and mean_r < -0.5:
            sugg = f"precedent unfavorable (mean_r={mean_r:+.2f}) -- consider STAND_ASIDE"
        else:
            sugg = f"precedent mixed (n={n})"
        return PrecedentQuery(
            key=key,
            n=n,
            win_rate=round(wr, 4) if wr is not None else None,
            median_r=round(median_r, 4) if median_r is not None else None,
            mean_r=round(mean_r, 4) if mean_r is not None else None,
            last_action=last.action,
            last_ts=last.ts,
            actions_freq=freq,
            suggestion=sugg,
        )

    def keys(self) -> list[PrecedentKey]:
        out: list[PrecedentKey] = []
        for k in self._store:
            out.append(
                PrecedentKey(
                    regime=k[0],
                    session_phase=k[1],
                    event_category=k[2],
                    binding_constraint=k[3],
                )
            )
        return out

    def size(self) -> int:
        return sum(len(v) for v in self._store.values())

    # Persistence -------------------------------------------------------
    def save(self, path: Path | str) -> None:
        out = {
            "max_per_bucket": self.max_per_bucket,
            "buckets": [
                {
                    "key": list(k),
                    "entries": [e.model_dump(mode="json") for e in list(v)],
                }
                for k, v in self._store.items()
            ],
        }
        Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> PrecedentGraph:
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        inst = cls(max_per_bucket=int(data.get("max_per_bucket", 200)))
        for b in data.get("buckets", []):
            k = tuple(b["key"])  # type: ignore[assignment]
            entries = [PrecedentEntry.model_validate(e) for e in b.get("entries", [])]
            inst._store[k] = deque(entries, maxlen=inst.max_per_bucket)
        return inst


def timestamp_utc() -> datetime:
    return datetime.now(UTC)
