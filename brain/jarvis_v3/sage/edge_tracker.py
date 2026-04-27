"""Per-school edge tracker (Wave-5 #3 + #4, 2026-04-27).

Tracks each school's rolling track record:
  * hit_rate -- fraction of times the school's bias matched the
    realized direction (priced N bars later)
  * avg_R -- average R-multiple of trades the school AGREED with
  * conviction_calibration -- when school says "0.7 conviction long",
    is the realized R actually positive 70% of the time?
  * n_obs -- sample count

Persists to ``state/sage/edge_tracker.json`` so it survives restarts.

The confluence aggregator can use the learned hit_rate as a multiplier
on the school's static WEIGHT: schools that have been right earn more
say; schools that have been wrong get muted automatically.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path(__file__).resolve().parents[3] / "state" / "sage" / "edge_tracker.json"


@dataclass
class SchoolEdge:
    """Rolling edge stats for a single school."""

    school: str
    n_obs: int = 0
    n_aligned_wins: int = 0      # bias matched + realized R > 0
    n_aligned_losses: int = 0    # bias matched + realized R <= 0
    sum_r: float = 0.0           # sum of realized R for trades school agreed with
    last_updated: str = ""

    @property
    def hit_rate(self) -> float:
        """Win rate among trades the school AGREED with."""
        n = self.n_aligned_wins + self.n_aligned_losses
        return self.n_aligned_wins / n if n > 0 else 0.5

    @property
    def avg_r(self) -> float:
        n = self.n_aligned_wins + self.n_aligned_losses
        return self.sum_r / n if n > 0 else 0.0

    @property
    def expectancy(self) -> float:
        """Average R-multiple per aligned trade (captures both hit-rate
        AND R-magnitude in one number).

        Equivalent to avg_r; named ``expectancy`` for the conventional
        meaning. When all trades lose, this is negative; when all win,
        positive; when none yet, zero.
        """
        return self.avg_r

    def weight_modifier(self) -> float:
        """0.5 to 1.5 weight modifier for the confluence aggregator.

        Schools with no track record return 1.0 (neutral). Schools with
        positive expectancy earn up-weight; negative expectancy earns
        down-weight. Bounded so a single bad sample can't crater a
        school's contribution.
        """
        if self.n_obs < 10:
            return 1.0  # not enough samples to judge
        # tanh-bounded: expectancy 0 -> 1.0; +0.5R -> ~1.4; -0.5R -> ~0.6
        from math import tanh
        return 1.0 + 0.5 * tanh(self.expectancy)


class EdgeTracker:
    """Thread-safe per-school edge tracker with JSON persistence."""

    def __init__(self, state_path: Path = DEFAULT_STATE_PATH) -> None:
        self.state_path = state_path
        self._lock = threading.Lock()
        self._edges: dict[str, SchoolEdge] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            for name, snap in data.get("edges", {}).items():
                self._edges[name] = SchoolEdge(
                    school=name,
                    n_obs=int(snap.get("n_obs", 0)),
                    n_aligned_wins=int(snap.get("n_aligned_wins", 0)),
                    n_aligned_losses=int(snap.get("n_aligned_losses", 0)),
                    sum_r=float(snap.get("sum_r", 0.0)),
                    last_updated=snap.get("last_updated", ""),
                )
        except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
            logger.warning("edge tracker load failed: %s", exc)

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.state_path.write_text(json.dumps({
                "saved_at": datetime.now(UTC).isoformat(),
                "edges": {
                    name: {
                        "n_obs": e.n_obs,
                        "n_aligned_wins": e.n_aligned_wins,
                        "n_aligned_losses": e.n_aligned_losses,
                        "sum_r": e.sum_r,
                        "hit_rate": e.hit_rate,
                        "avg_r": e.avg_r,
                        "expectancy": e.expectancy,
                        "weight_modifier": e.weight_modifier(),
                        "last_updated": e.last_updated,
                    }
                    for name, e in self._edges.items()
                },
            }, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("edge tracker save failed: %s", exc)

    def observe(
        self,
        *,
        school: str,
        school_bias: str,    # "long" | "short" | "neutral"
        entry_side: str,     # the trade actually taken
        realized_r: float,
    ) -> None:
        """Record one observation. Bot calls this after each trade
        closes, for every school in the SageReport."""
        with self._lock:
            e = self._edges.setdefault(school, SchoolEdge(school=school))
            e.n_obs += 1
            # Did the school AGREE with the entry side?
            agreed = (
                (school_bias == "long" and entry_side == "long")
                or (school_bias == "short" and entry_side == "short")
            )
            if agreed:
                e.sum_r += realized_r
                if realized_r > 0:
                    e.n_aligned_wins += 1
                else:
                    e.n_aligned_losses += 1
            # If school was neutral or disagreed, just bump n_obs (sample
            # of activity but not a trade attribution)
            e.last_updated = datetime.now(UTC).isoformat()
            self._save()

    def edge_for(self, school: str) -> SchoolEdge:
        """Return the SchoolEdge for `school`. Returns an empty one if
        not yet observed."""
        with self._lock:
            return self._edges.get(school) or SchoolEdge(school=school)

    def all_weight_modifiers(self) -> dict[str, float]:
        """Snapshot of every tracked school's weight_modifier."""
        with self._lock:
            return {name: e.weight_modifier() for name, e in self._edges.items()}

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Read-only view of every school's stats."""
        with self._lock:
            return {
                name: {
                    "n_obs": e.n_obs,
                    "hit_rate": round(e.hit_rate, 4),
                    "avg_r": round(e.avg_r, 4),
                    "expectancy": round(e.expectancy, 4),
                    "weight_modifier": round(e.weight_modifier(), 4),
                    "last_updated": e.last_updated,
                }
                for name, e in self._edges.items()
            }


# Module-level singleton.
_default: EdgeTracker | None = None


def default_tracker() -> EdgeTracker:
    global _default
    if _default is None:
        _default = EdgeTracker()
    return _default
