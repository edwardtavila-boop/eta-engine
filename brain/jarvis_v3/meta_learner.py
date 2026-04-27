"""Meta-learning + self-architecture evolution (Wave-8 #7, 2026-04-27).

Mutates JARVIS's own hyperparameters and architectural choices, runs
candidates in shadow mode, and promotes winners. The evolutionary
loop the audit list calls for.

What it mutates:
  * Bandit epsilon (exploration rate)
  * JARVIS confidence threshold (approve-vs-defer cutoff)
  * Sage confluence weights (per-school weighting)
  * Filter-bandit arm allocation
  * Risk-committee severity (firm board)

How it works (the evolutionary loop):
  1. Snapshot current "champion" config
  2. Mutate one hyperparameter per challenger (small perturbation)
  3. Run challenger in SHADOW mode -- it records what it WOULD have
     decided, but does NOT replace the champion's actions
  4. After N trades, compare realized-R distributions
  5. Promote challenger only if it beats champion by >= configured
     margin AND has at least min_episodes of data
  6. Record lineage in HierarchicalMemory.procedural

Use case (run as a cron job):

    from eta_engine.brain.jarvis_v3.meta_learner import MetaLearner

    ml = MetaLearner.default()
    new_version = ml.tick(memory=hierarchical_memory)
    if new_version is not None:
        logger.info("promoted: %s -> %s", new_version.parent_id,
                    new_version.version_id)

DESIGN INTENT: this is offline / advisory by default. The mutation
is constrained (small steps, all from a whitelist), and promotion
requires a clean improvement margin. Operator can disable promotion
entirely by setting ``auto_promote=False``.
"""
from __future__ import annotations

import logging
import random
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import (
        HierarchicalMemory,
        ProceduralVersion,
    )

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHAMPION_PATH = ROOT / "state" / "meta_learner" / "champion.json"


# ─── Config space ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ParamBounds:
    """Whitelist of mutable hyperparameters with safe ranges.

    The keys here MUST be the only parameters the mutator touches --
    keeps the search space narrow and safe.
    """

    bandit_epsilon: tuple[float, float] = (0.02, 0.20)
    jarvis_confidence_threshold: tuple[float, float] = (0.45, 0.75)
    sage_wyckoff_weight: tuple[float, float] = (0.5, 1.5)
    sage_elliott_weight: tuple[float, float] = (0.5, 1.5)
    sage_dow_weight: tuple[float, float] = (0.5, 1.5)
    sage_auction_weight: tuple[float, float] = (0.5, 1.5)
    filter_bandit_epsilon: tuple[float, float] = (0.05, 0.25)
    risk_committee_severity: tuple[float, float] = (0.5, 1.5)


@dataclass
class CandidateConfig:
    """A specific hyperparameter snapshot."""

    bandit_epsilon: float = 0.10
    jarvis_confidence_threshold: float = 0.60
    sage_wyckoff_weight: float = 1.0
    sage_elliott_weight: float = 1.0
    sage_dow_weight: float = 1.0
    sage_auction_weight: float = 1.0
    filter_bandit_epsilon: float = 0.10
    risk_committee_severity: float = 1.0

    def to_dict(self) -> dict:
        return {
            "bandit_epsilon": self.bandit_epsilon,
            "jarvis_confidence_threshold": self.jarvis_confidence_threshold,
            "sage_wyckoff_weight": self.sage_wyckoff_weight,
            "sage_elliott_weight": self.sage_elliott_weight,
            "sage_dow_weight": self.sage_dow_weight,
            "sage_auction_weight": self.sage_auction_weight,
            "filter_bandit_epsilon": self.filter_bandit_epsilon,
            "risk_committee_severity": self.risk_committee_severity,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CandidateConfig:
        return cls(
            bandit_epsilon=float(d.get("bandit_epsilon", 0.10)),
            jarvis_confidence_threshold=float(d.get("jarvis_confidence_threshold", 0.60)),
            sage_wyckoff_weight=float(d.get("sage_wyckoff_weight", 1.0)),
            sage_elliott_weight=float(d.get("sage_elliott_weight", 1.0)),
            sage_dow_weight=float(d.get("sage_dow_weight", 1.0)),
            sage_auction_weight=float(d.get("sage_auction_weight", 1.0)),
            filter_bandit_epsilon=float(d.get("filter_bandit_epsilon", 0.10)),
            risk_committee_severity=float(d.get("risk_committee_severity", 1.0)),
        )


# ─── Mutation ─────────────────────────────────────────────────────

# Module-level singleton: ParamBounds is frozen, so this is safe to
# share across threads/calls. (Avoids B008 "call in default args".)
_DEFAULT_BOUNDS = ParamBounds()


def mutate(
    cfg: CandidateConfig,
    *,
    bounds: ParamBounds | None = None,
    n_mutations: int = 1,
    step_pct: float = 0.10,
    rng: random.Random | None = None,
) -> CandidateConfig:
    """Return a perturbed copy. Mutates ``n_mutations`` randomly-
    chosen parameters by a uniform step of +/-step_pct around their
    current value, clamped to bounds."""
    bounds = bounds or _DEFAULT_BOUNDS
    rng = rng or random.Random()
    keys = list(cfg.to_dict().keys())
    chosen = rng.sample(keys, k=min(n_mutations, len(keys)))
    new_d = cfg.to_dict()
    for k in chosen:
        cur = float(new_d[k])
        lo, hi = getattr(bounds, k)
        delta = rng.uniform(-step_pct, +step_pct) * cur
        nxt = cur + delta
        new_d[k] = max(lo, min(hi, nxt))
    return CandidateConfig.from_dict(new_d)


# ─── Shadow trial ─────────────────────────────────────────────────


@dataclass
class ShadowTrial:
    """One challenger config running in shadow mode against the
    champion. Records the realized_r each time the operator-level
    decision goes through and the trial's hypothetical action would
    have differed."""

    challenger_id: str
    challenger_cfg: CandidateConfig
    realized_r_observations: list[float] = field(default_factory=list)
    started_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )

    @property
    def n(self) -> int:
        return len(self.realized_r_observations)

    @property
    def avg_r(self) -> float:
        if not self.realized_r_observations:
            return 0.0
        return sum(self.realized_r_observations) / len(self.realized_r_observations)


# ─── Main loop ────────────────────────────────────────────────────


@dataclass
class MetaLearnerConfig:
    """Operator-tunable knobs for the evolutionary loop."""

    promotion_margin_r: float = 0.20    # challenger must beat champion by >= 0.20R avg
    min_episodes: int = 30               # before any promotion is considered
    n_challengers: int = 3               # how many challengers per generation
    auto_promote: bool = False           # if False, only PROPOSES; operator confirms


class MetaLearner:
    """Manages champion config, generates challengers, evaluates,
    and (optionally) auto-promotes winners."""

    def __init__(
        self,
        *,
        cfg: MetaLearnerConfig | None = None,
        bounds: ParamBounds | None = None,
        champion_path: Path = DEFAULT_CHAMPION_PATH,
    ) -> None:
        self.cfg = cfg or MetaLearnerConfig()
        self.bounds = bounds or _DEFAULT_BOUNDS
        self.champion_path = champion_path
        self._champion: CandidateConfig = self._load_champion()
        self._champion_id: str = "v0_genesis"
        self._trials: dict[str, ShadowTrial] = {}

    @classmethod
    def default(cls) -> MetaLearner:
        return cls()

    def champion(self) -> CandidateConfig:
        return self._champion

    def champion_id(self) -> str:
        return self._champion_id

    def spawn_challengers(self) -> list[ShadowTrial]:
        """Create n new challengers, each a one-mutation perturbation
        of the current champion."""
        rng = random.Random(secrets.randbits(64))
        new = []
        for i in range(self.cfg.n_challengers):
            cid = f"c_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{i}"
            mutated = mutate(self._champion, bounds=self.bounds, rng=rng)
            trial = ShadowTrial(challenger_id=cid, challenger_cfg=mutated)
            self._trials[cid] = trial
            new.append(trial)
        return new

    def record_shadow_outcome(self, challenger_id: str, realized_r: float) -> None:
        trial = self._trials.get(challenger_id)
        if trial is None:
            return
        trial.realized_r_observations.append(float(realized_r))

    def evaluate_and_promote(
        self,
        *,
        memory: HierarchicalMemory | None = None,
        champion_avg_r: float = 0.0,
    ) -> ProceduralVersion | None:
        """Pick the best challenger that meets the promotion bar and
        promote it. Returns the new ProceduralVersion or None.

        ``champion_avg_r`` should be the realized avg R the live
        champion has produced over the same window. The challenger
        must beat champion by ``promotion_margin_r`` AND have at
        least ``min_episodes`` observations.
        """
        eligible = [
            t for t in self._trials.values()
            if t.n >= self.cfg.min_episodes
        ]
        if not eligible:
            return None
        best = max(eligible, key=lambda t: t.avg_r)
        if best.avg_r - champion_avg_r < self.cfg.promotion_margin_r:
            return None
        if not self.cfg.auto_promote:
            logger.info(
                "meta-learner: challenger %s beats champion %s by %.3f R "
                "(margin %.2f); auto_promote=False, recording proposal only",
                best.challenger_id, self._champion_id,
                best.avg_r - champion_avg_r, self.cfg.promotion_margin_r,
            )
            if memory is not None:
                v = memory.record_procedural_version(
                    version_id=best.challenger_id,
                    parent_id=self._champion_id,
                    params=best.challenger_cfg.to_dict(),
                    realized_metric=best.avg_r,
                    notes="proposed (auto_promote=False); awaiting operator review",
                )
                return v
            return None
        # Auto-promote
        prior_id = self._champion_id
        self._champion = best.challenger_cfg
        self._champion_id = best.challenger_id
        self._save_champion()
        if memory is not None:
            return memory.record_procedural_version(
                version_id=best.challenger_id,
                parent_id=prior_id,
                params=best.challenger_cfg.to_dict(),
                realized_metric=best.avg_r,
                notes=f"promoted from {prior_id}",
            )
        return None

    def tick(
        self,
        *,
        memory: HierarchicalMemory | None = None,
        champion_avg_r: float = 0.0,
    ) -> ProceduralVersion | None:
        """Convenience: evaluate any pending trials, then spawn the
        next generation. Returns the newly-promoted version, if any."""
        promoted = self.evaluate_and_promote(
            memory=memory, champion_avg_r=champion_avg_r,
        )
        # Clean up promoted trials so they don't compete again
        if promoted:
            self._trials.pop(promoted.version_id, None)
        # Garbage-collect any trials with too many observations to be
        # useful anymore (they've had their evaluation window)
        for tid, trial in list(self._trials.items()):
            if trial.n > self.cfg.min_episodes * 3:
                self._trials.pop(tid)
        # Spawn next batch
        self.spawn_challengers()
        return promoted

    # ── Persistence ────────────────────────────────────────────

    def _load_champion(self) -> CandidateConfig:
        if not self.champion_path.exists():
            return CandidateConfig()
        try:
            import json
            data = json.loads(self.champion_path.read_text(encoding="utf-8"))
            return CandidateConfig.from_dict(data)
        except (OSError, ValueError) as exc:
            logger.warning(
                "meta-learner: champion load failed (%s); fresh defaults", exc,
            )
            return CandidateConfig()

    def _save_champion(self) -> None:
        try:
            import json
            self.champion_path.parent.mkdir(parents=True, exist_ok=True)
            self.champion_path.write_text(
                json.dumps(self._champion.to_dict(), indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("meta-learner: champion save failed (%s)", exc)
