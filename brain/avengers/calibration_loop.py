"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.calibration_loop
==================================================
Per-persona, per-category scoreboard that learns which Avenger was
actually right in the past and weights future votes accordingly.

Why this exists
---------------
Today every persona's verdict carries equal weight (when the Fleet
pools them) or whatever static weight the caller hard-coded. That's
wrong over time: Batman might be better at ADVERSARIAL_REVIEW than
Alfred, but for STRATEGY_EDIT, Alfred might ship more wins. We track
success rates per (persona, category) and expose a ``weight()`` method
that downstream code can multiply into its vote tally.

Design
------
* Append-only JSONL outcome log at ``~/.jarvis/calibration.jsonl``.
* ``PersonaScore`` tracks (successes, failures, last_seen) for one
  (persona, category) bucket.
* ``CalibrationLoop.record(result)`` is the Fleet hook -- called from
  HardenedFleet after every dispatch.
* ``CalibrationLoop.weight(persona, category)`` returns a Laplace-smoothed
  success rate in [0.1, 1.0]. Never zero (we want exploration to stay
  possible even after a bad streak).
"""

from __future__ import annotations

import contextlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.avengers.base import PersonaId, TaskEnvelope, TaskResult
from eta_engine.brain.model_policy import TaskCategory

CALIBRATION_JOURNAL: Path = Path.home() / ".jarvis" / "calibration.jsonl"


class PersonaScore(BaseModel):
    """One (persona, category) running score."""

    model_config = ConfigDict(frozen=False)

    persona: str
    category: str
    successes: int = Field(ge=0, default=0)
    failures: int = Field(ge=0, default=0)
    last_seen: datetime | None = None

    @property
    def total(self) -> int:
        return self.successes + self.failures

    @property
    def rate(self) -> float:
        """Laplace-smoothed success rate in [0.1, 1.0]."""
        # (s + 1) / (s + f + 2), floored at 0.1 so a cold persona still
        # gets ~0.5 and a persona on a bad run stays > 0.
        raw = (self.successes + 1) / (self.total + 2)
        return max(0.1, min(1.0, raw))


class CalibrationLoop:
    """Shared scoreboard + append-only log.

    Parameters
    ----------
    journal_path
        JSONL outcome log. Defaults to ``~/.jarvis/calibration.jsonl``.
    rehydrate
        If True, load existing journal on init. Tests pass False.
    """

    def __init__(
        self,
        journal_path: Path | None = None,
        *,
        rehydrate: bool = True,
    ) -> None:
        self.journal_path = journal_path or CALIBRATION_JOURNAL
        self._scores: dict[tuple[str, str], PersonaScore] = {}
        self._by_persona: dict[str, int] = defaultdict(int)
        if rehydrate:
            self._rehydrate()

    def _rehydrate(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            for raw in self.journal_path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                persona = rec.get("persona", "")
                category = rec.get("category", "")
                if not persona or not category:
                    continue
                key = (persona, category)
                score = self._scores.get(key) or PersonaScore(
                    persona=persona,
                    category=category,
                )
                if rec.get("success"):
                    score.successes += 1
                else:
                    score.failures += 1
                ts_raw = rec.get("ts")
                if ts_raw:
                    with contextlib.suppress(ValueError):
                        score.last_seen = datetime.fromisoformat(
                            str(ts_raw).replace("Z", "+00:00"),
                        )
                self._scores[key] = score
                self._by_persona[persona] += 1
        except OSError:
            pass

    def record(
        self,
        envelope: TaskEnvelope,
        result: TaskResult,
    ) -> None:
        """Fleet hook: called after every dispatch.

        Takes both the envelope (for category) and the result (for the
        persona that actually ran and whether it succeeded). TaskResult
        alone is insufficient -- category lives on the envelope.
        """
        persona = result.persona_id.value
        category = envelope.category.value
        key = (persona, category)
        score = self._scores.get(key) or PersonaScore(
            persona=persona,
            category=category,
        )
        if result.success:
            score.successes += 1
        else:
            score.failures += 1
        score.last_seen = datetime.now(UTC)
        self._scores[key] = score
        self._by_persona[persona] += 1
        self._append(persona, category, result.success)

    def _append(self, persona: str, category: str, success: bool) -> None:
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "persona": persona,
                            "category": category,
                            "success": success,
                        }
                    )
                    + "\n"
                )
        except OSError:
            # Lossy append is survivable -- in-memory state wins.
            return

    def weight(
        self,
        persona: PersonaId | str,
        category: TaskCategory | str,
    ) -> float:
        """Return a multiplier in [0.1, 1.0]. Cold pairs get ~0.5."""
        p = persona.value if isinstance(persona, PersonaId) else persona
        c = category.value if isinstance(category, TaskCategory) else category
        score = self._scores.get((p, c))
        return score.rate if score else 0.5

    def snapshot(self) -> list[PersonaScore]:
        """Return a sorted copy of every bucket. For dashboards / CLI."""
        return sorted(
            self._scores.values(),
            key=lambda s: (s.persona, s.category),
        )


__all__ = [
    "CALIBRATION_JOURNAL",
    "CalibrationLoop",
    "PersonaScore",
]
