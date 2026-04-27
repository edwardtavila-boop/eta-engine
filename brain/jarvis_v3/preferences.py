"""
JARVIS v3 // preferences
========================
Operator-preference learning.

Every time the operator downgrades a CONDITIONAL response, bumps a size
cap, or overrides a DENIED verdict, it's a signal: JARVIS's default
policy was too loose or too tight. This module collects those signals,
aggregates them by (subsystem, action, reason_code), and produces
``PreferenceNudge`` objects that can be applied to future verdicts to
pre-empt the same manual override.

No ML here -- just exponentially-weighted running counters with a
trust threshold. The core insight: operators don't want a neural net,
they want "if I overrode this same gate 5 times in a week, stop firing
that gate."

Stdlib + pydantic only.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class OverrideEvent(BaseModel):
    """Single operator intervention on a Jarvis verdict."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    subsystem: str = Field(min_length=1)
    action: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    # direction: 'loosen' (operator approved what Jarvis denied), 'tighten'
    # (operator capped what Jarvis approved), 'veto' (operator flattened).
    direction: str = Field(pattern="^(loosen|tighten|veto)$")
    rationale: str = ""


class PreferenceNudge(BaseModel):
    """How to bias a future verdict for this (subsystem, action, reason_code)."""

    model_config = ConfigDict(frozen=True)

    subsystem: str
    action: str
    reason_code: str
    # Score in [-1, 1]. Positive = operator tends to loosen; negative = tighten.
    score: float = Field(ge=-1.0, le=1.0)
    sample_count: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    suggestion: str = Field(min_length=1)


class OperatorPreferenceLearner:
    """Exponentially-weighted tally of override events per composite key.

    Half-life defaults to 14 days -- operator preferences shift.
    """

    def __init__(self, half_life_days: float = 14.0) -> None:
        if half_life_days <= 0:
            raise ValueError("half_life_days must be > 0")
        self.half_life_days = half_life_days
        # key -> (loosen_weight, tighten_weight, last_ts)
        self._tally: dict[tuple[str, str, str], list[float]] = defaultdict(
            lambda: [0.0, 0.0, 0.0],
        )
        self._sample_count: dict[tuple[str, str, str], int] = defaultdict(int)

    def observe(self, event: OverrideEvent) -> None:
        key = (event.subsystem, event.action, event.reason_code)
        ts_s = event.ts.replace(tzinfo=event.ts.tzinfo or UTC).timestamp()
        bucket = self._tally[key]
        # Decay existing weights to the new ts.
        bucket[0] *= self._decay(bucket[2], ts_s)
        bucket[1] *= self._decay(bucket[2], ts_s)
        bucket[2] = ts_s
        if event.direction == "loosen":
            bucket[0] += 1.0
        elif event.direction == "tighten":
            bucket[1] += 1.0
        elif event.direction == "veto":
            bucket[1] += 2.0
        self._sample_count[key] += 1

    def nudge_for(
        self,
        subsystem: str,
        action: str,
        reason_code: str,
        now: datetime | None = None,
    ) -> PreferenceNudge | None:
        key = (subsystem, action, reason_code)
        if key not in self._tally:
            return None
        loosen, tighten, last = self._tally[key]
        if now is not None:
            now_s = now.replace(tzinfo=now.tzinfo or UTC).timestamp()
            d = self._decay(last, now_s)
            loosen *= d
            tighten *= d
        total = loosen + tighten
        if total <= 0.0:
            return None
        raw = (loosen - tighten) / total
        n = self._sample_count[key]
        # Confidence plateaus once we have >= 10 relevant samples.
        confidence = min(1.0, n / 10.0)
        if raw > 0:
            suggestion = f"operator tends to loosen ({raw:+.2f}) -- upgrade verdict"
        else:
            suggestion = f"operator tends to tighten ({raw:+.2f}) -- cap size or deny"
        return PreferenceNudge(
            subsystem=subsystem,
            action=action,
            reason_code=reason_code,
            score=round(raw, 4),
            sample_count=n,
            confidence=round(confidence, 4),
            suggestion=suggestion,
        )

    def _decay(self, last_s: float, now_s: float) -> float:
        if last_s <= 0.0 or now_s <= last_s:
            return 1.0
        dt_days = (now_s - last_s) / 86400.0
        return math.exp(-math.log(2) * dt_days / self.half_life_days)

    # Persistence -------------------------------------------------------
    def save(self, path: Path | str) -> None:
        out = {
            "half_life_days": self.half_life_days,
            "tally": [
                {
                    "key": list(k),
                    "loosen": v[0],
                    "tighten": v[1],
                    "last_ts": v[2],
                    "samples": self._sample_count[k],
                }
                for k, v in self._tally.items()
            ],
        }
        Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> OperatorPreferenceLearner:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        inst = cls(half_life_days=float(data.get("half_life_days", 14.0)))
        for rec in data.get("tally", []):
            key = tuple(rec["key"])  # type: ignore[assignment]
            inst._tally[key] = [
                float(rec.get("loosen", 0.0)),
                float(rec.get("tighten", 0.0)),
                float(rec.get("last_ts", 0.0)),
            ]
            inst._sample_count[key] = int(rec.get("samples", 0))
        return inst
