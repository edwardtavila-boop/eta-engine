"""Operator coach (Wave-14, 2026-04-27).

Learns the operator's override patterns over time so JARVIS can
anticipate when the operator will disagree.

Mechanism (per (regime, session, action) cell):
  * Beta-Bernoulli posterior on P(operator overrides JARVIS)
  * Records: (alpha=overrides, beta=acceptances) per cell
  * `should_defer_to_operator(proposal)` returns probability that the
    operator will override + a recommended pre-emptive softening

This is an ADVISORY layer -- it does not block JARVIS. It feeds:
  * narrative_generator: "Operator overrides JARVIS in this cell 70%
    of the time; consider escalating to operator review."
  * intelligence layer (optional): when override prob is high, JARVIS
    can pre-emptively shrink size to make the operator override less
    necessary.

Pure stdlib + math.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_PATH = ROOT / "state" / "jarvis_intel" / "operator_coach.json"


@dataclass
class CellPosterior:
    """Beta posterior for one (regime, session, action) cell."""

    regime: str
    session: str
    action: str
    alpha: int = 1  # Beta(1,1) prior = uniform
    beta: int = 1
    last_updated: str = ""

    @property
    def n_observations(self) -> int:
        # alpha + beta - 2 because we start at (1, 1)
        return max(0, self.alpha + self.beta - 2)

    @property
    def override_probability(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def update(self, *, was_overridden: bool) -> None:
        if was_overridden:
            self.alpha += 1
        else:
            self.beta += 1
        self.last_updated = datetime.now(UTC).isoformat()


@dataclass
class CoachAdvice:
    """Output of should_defer_to_operator()."""

    override_probability: float
    n_observations: int
    recommendation: str  # "auto_proceed" / "soften" / "escalate"
    suggested_size_shrink: float  # multiplier, 1.0 = no shrink
    note: str = ""


# ─── Coach ────────────────────────────────────────────────────────


class OperatorCoach:
    """Persistent Beta-Bernoulli learner over operator overrides."""

    def __init__(self, *, state_path: Path = DEFAULT_STATE_PATH) -> None:
        self.state_path = state_path
        self._cells: dict[str, CellPosterior] = {}
        self._load()

    @classmethod
    def default(cls) -> OperatorCoach:
        return cls()

    @staticmethod
    def _key(regime: str, session: str, action: str) -> str:
        return f"{regime}|{session}|{action}"

    def record_outcome(
        self,
        *,
        regime: str,
        session: str,
        action: str,
        was_overridden: bool,
    ) -> CellPosterior:
        """Update the posterior for one observed approval -> outcome."""
        k = self._key(regime, session, action)
        cell = self._cells.get(k)
        if cell is None:
            cell = CellPosterior(regime=regime, session=session, action=action)
            self._cells[k] = cell
        cell.update(was_overridden=was_overridden)
        self._save()
        return cell

    def should_defer_to_operator(
        self,
        *,
        regime: str,
        session: str,
        action: str,
    ) -> CoachAdvice:
        """Return advice based on the cell's posterior.

        Rules:
          * < 5 observations -> auto_proceed (insufficient data)
          * override prob < 0.30 -> auto_proceed
          * 0.30 <= prob < 0.60 -> soften (shrink size)
          * prob >= 0.60 -> escalate to operator review
        """
        k = self._key(regime, session, action)
        cell = self._cells.get(k)
        if cell is None or cell.n_observations < 5:
            return CoachAdvice(
                override_probability=0.0,
                n_observations=0 if cell is None else cell.n_observations,
                recommendation="auto_proceed",
                suggested_size_shrink=1.0,
                note="insufficient observation history",
            )
        p = cell.override_probability
        if p < 0.30:
            return CoachAdvice(
                override_probability=round(p, 3),
                n_observations=cell.n_observations,
                recommendation="auto_proceed",
                suggested_size_shrink=1.0,
                note=f"operator rarely overrides this cell ({p:.0%})",
            )
        if p < 0.60:
            shrink = max(0.5, 1.0 - (p - 0.30) * 1.5)
            return CoachAdvice(
                override_probability=round(p, 3),
                n_observations=cell.n_observations,
                recommendation="soften",
                suggested_size_shrink=round(shrink, 3),
                note=(f"operator overrides {p:.0%} in this cell; shrink to {shrink:.0%}"),
            )
        return CoachAdvice(
            override_probability=round(p, 3),
            n_observations=cell.n_observations,
            recommendation="escalate",
            suggested_size_shrink=0.0,
            note=(f"operator overrides {p:.0%} in this cell; escalate before acting"),
        )

    def report(self) -> list[dict]:
        return sorted(
            [
                {
                    "regime": c.regime,
                    "session": c.session,
                    "action": c.action,
                    "n_observations": c.n_observations,
                    "override_probability": round(c.override_probability, 3),
                    "last_updated": c.last_updated,
                }
                for c in self._cells.values()
            ],
            key=lambda d: d["override_probability"],
            reverse=True,
        )

    # ── Persistence ──────────────────────────────────────────

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            for k, raw in data.items():
                self._cells[k] = CellPosterior(**raw)
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("operator_coach: load failed (%s)", exc)

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(
                    {k: asdict(c) for k, c in self._cells.items()},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("operator_coach: save failed (%s)", exc)
