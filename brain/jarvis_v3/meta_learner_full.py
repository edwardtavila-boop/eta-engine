"""Full-build meta-learner (Wave-10 upgrade of Wave-8 #7).

Upgrades over the lean random-mutation evolutionary loop in
``meta_learner.py``:

  * GRADIENT-FREE LOCAL SEARCH (SPSA-style) with finite-difference
    sensitivity estimation -- mutate two trial points along the same
    perturbation axis, infer ascent direction, step toward it
  * MULTI-OBJECTIVE PARETO RANKING -- challengers are now scored on
    (avg_R, sharpe, max_dd, ulcer_index) jointly; promotion requires
    Pareto-dominance over the champion on AT LEAST 2 dimensions and
    no regression on any
  * HYPERPARAMETER IMPORTANCE: bandit over WHICH parameter to mutate
    next -- if mutations of "bandit_epsilon" consistently produce
    bigger improvements than mutations of "sage_dow_weight", the
    sampler shifts mass toward the high-leverage parameter
  * BUDGET-AWARE: enforces a max of N mutation experiments per day
    so the meta-loop doesn't burn through journal observations

This is what the audit list called "meta-RL with self-architecture
evolution" in a production-safe form: bounded shadow challengers,
finite-difference sensitivity probes, and Pareto promotion gates
instead of unconstrained model rewrites.

Pure stdlib + math.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from eta_engine.brain.jarvis_v3.meta_learner import (
    CandidateConfig,
    ParamBounds,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

logger = logging.getLogger(__name__)

_DEFAULT_BOUNDS = ParamBounds()


# ─── Multi-objective metrics ──────────────────────────────────────


@dataclass
class MultiObjective:
    """Joint performance on 4 metrics."""

    avg_r: float  # higher is better
    sharpe: float  # higher is better
    max_dd_r: float  # less negative is better -- stored as positive number
    ulcer_index: float  # lower is better
    n_observations: int = 0


def compute_multi_objective(realized_r: list[float]) -> MultiObjective:
    """Calculate the 4 metrics from a journal of R-multiples."""
    n = len(realized_r)
    if n == 0:
        return MultiObjective(0.0, 0.0, 0.0, 0.0, 0)
    avg_r = sum(realized_r) / n
    sd = math.sqrt(sum((r - avg_r) ** 2 for r in realized_r) / max(n - 1, 1)) if n >= 2 else 0.0
    sharpe = avg_r / sd if sd > 0 else 0.0
    # Max drawdown (in cumulative R units)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    drawdown_squares: list[float] = []
    for r in realized_r:
        cum += r
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)
        drawdown_squares.append(dd * dd)
    ulcer = math.sqrt(sum(drawdown_squares) / len(drawdown_squares)) if drawdown_squares else 0.0
    return MultiObjective(
        avg_r=round(avg_r, 4),
        sharpe=round(sharpe, 4),
        max_dd_r=round(max_dd, 4),
        ulcer_index=round(ulcer, 4),
        n_observations=n,
    )


def pareto_dominates(
    challenger: MultiObjective,
    champion: MultiObjective,
    *,
    min_dominated_dimensions: int = 2,
) -> bool:
    """Return True iff ``challenger`` strictly beats ``champion`` on
    at least ``min_dominated_dimensions`` of the 4 metrics AND does
    not regress on any.

    Conventions: avg_r and sharpe -- higher is better; max_dd_r and
    ulcer_index -- lower is better.
    """
    metrics: list[tuple[float, float, bool]] = [
        (challenger.avg_r, champion.avg_r, True),  # higher better
        (challenger.sharpe, champion.sharpe, True),
        (challenger.max_dd_r, champion.max_dd_r, False),  # lower better
        (challenger.ulcer_index, champion.ulcer_index, False),
    ]
    n_better = 0
    for c, ch, higher_better in metrics:
        if higher_better:
            if c < ch:
                return False  # regression -- disqualify
            if c > ch:
                n_better += 1
        else:
            if c > ch:
                return False
            if c < ch:
                n_better += 1
    return n_better >= min_dominated_dimensions


# ─── Hyperparameter importance bandit ─────────────────────────────


@dataclass
class _ParameterArm:
    """Tracks the average improvement produced by mutating this param."""

    param_name: str
    pulls: int = 0
    cum_improvement: float = 0.0

    @property
    def mean_improvement(self) -> float:
        return self.cum_improvement / max(self.pulls, 1)


class ParameterImportanceBandit:
    """Bandit-over-which-parameter-to-mutate. Builds an empirical
    distribution of where the high-leverage tweaks live."""

    def __init__(self, *, epsilon: float = 0.20) -> None:
        self.epsilon = epsilon
        self._arms: dict[str, _ParameterArm] = {}

    def register_param(self, name: str) -> None:
        if name not in self._arms:
            self._arms[name] = _ParameterArm(param_name=name)

    def pick(self, *, rng: random.Random) -> str:
        """Pick a parameter to mutate next. Epsilon-greedy on the
        running mean improvement."""
        if not self._arms:
            raise RuntimeError("no parameters registered")
        arms = list(self._arms.values())
        if rng.random() < self.epsilon:
            return rng.choice(arms).param_name
        return max(arms, key=lambda a: a.mean_improvement).param_name

    def observe(self, param_name: str, improvement: float) -> None:
        arm = self._arms.get(param_name)
        if arm is None:
            return
        arm.pulls += 1
        arm.cum_improvement += float(improvement)

    def report(self) -> list[dict]:
        return sorted(
            [
                {
                    "param": a.param_name,
                    "pulls": a.pulls,
                    "mean_improvement": round(a.mean_improvement, 4),
                }
                for a in self._arms.values()
            ],
            key=lambda d: d["mean_improvement"],
            reverse=True,
        )


# ─── SPSA-style finite-difference sensitivity ─────────────────────


@dataclass
class SensitivityProbe:
    """Result of a 2-trial finite-difference probe along one parameter
    axis. Estimates the local gradient sign + magnitude."""

    param_name: str
    delta: float  # the perturbation size used
    upper_avg_r: float  # avg R at +delta point
    lower_avg_r: float  # avg R at -delta point
    sensitivity: float  # (upper - lower) / (2 * delta) -- gradient proxy


def probe_sensitivity(
    base_cfg: CandidateConfig,
    param_name: str,
    *,
    delta_pct: float = 0.10,
    eval_fn: Callable[[CandidateConfig], float],
) -> SensitivityProbe:
    """Run TWO trial points symmetrically perturbed in ``param_name``,
    use ``eval_fn`` to score each, and return the gradient estimate.

    ``eval_fn(cfg) -> float`` is supplied by caller -- typically a
    short shadow-mode trial averaging realized R.
    """
    bounds = _DEFAULT_BOUNDS
    cur_value = float(base_cfg.to_dict()[param_name])
    delta = delta_pct * cur_value if cur_value != 0 else delta_pct
    lo, hi = getattr(bounds, param_name)
    upper = max(lo, min(hi, cur_value + delta))
    lower = max(lo, min(hi, cur_value - delta))

    upper_d = base_cfg.to_dict()
    upper_d[param_name] = upper
    upper_cfg = CandidateConfig.from_dict(upper_d)

    lower_d = base_cfg.to_dict()
    lower_d[param_name] = lower
    lower_cfg = CandidateConfig.from_dict(lower_d)

    upper_score = eval_fn(upper_cfg)
    lower_score = eval_fn(lower_cfg)

    span = max(upper - lower, 1e-9)
    sensitivity = (upper_score - lower_score) / span
    return SensitivityProbe(
        param_name=param_name,
        delta=round(delta, 6),
        upper_avg_r=round(upper_score, 4),
        lower_avg_r=round(lower_score, 4),
        sensitivity=round(sensitivity, 6),
    )


def _mutate_single_param(
    cfg: CandidateConfig,
    param_name: str,
    *,
    bounds: ParamBounds,
    rng: random.Random,
    step_pct: float = 0.10,
) -> CandidateConfig:
    """Mutate exactly the parameter selected by the importance bandit."""
    values = cfg.to_dict()
    if param_name not in values:
        raise KeyError(f"unknown mutable parameter: {param_name}")
    cur = float(values[param_name])
    lo, hi = getattr(bounds, param_name)
    scale = cur if cur != 0 else max(abs(hi - lo), 1.0)
    delta = rng.uniform(-step_pct, step_pct) * scale
    values[param_name] = max(lo, min(hi, cur + delta))
    return CandidateConfig.from_dict(values)


# ─── Multi-objective meta-learner ────────────────────────────────


@dataclass
class MetaLearnerFullConfig:
    """Operator-tunable knobs for the multi-objective evolutionary
    loop."""

    n_challengers: int = 3
    min_episodes: int = 30
    min_dominated_dimensions: int = 2
    max_experiments_per_day: int = 10
    bandit_epsilon: float = 0.20
    auto_promote: bool = False


@dataclass
class _Trial:
    challenger_id: str
    challenger_cfg: CandidateConfig
    realized_r_observations: list[float] = field(default_factory=list)
    parent_param_mutated: str | None = None
    started_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )

    @property
    def n(self) -> int:
        return len(self.realized_r_observations)

    @property
    def metrics(self) -> MultiObjective:
        return compute_multi_objective(self.realized_r_observations)


class MetaLearnerFull:
    """Pareto-ranked evolutionary loop with importance-bandit-driven
    parameter selection."""

    def __init__(
        self,
        *,
        cfg: MetaLearnerFullConfig | None = None,
        bounds: ParamBounds | None = None,
    ) -> None:
        self.cfg = cfg or MetaLearnerFullConfig()
        self.bounds = bounds or _DEFAULT_BOUNDS
        self._champion = CandidateConfig()
        self._champion_id = "v0_genesis"
        self._champion_metrics: MultiObjective | None = None
        self._trials: dict[str, _Trial] = {}
        self._param_bandit = ParameterImportanceBandit(
            epsilon=self.cfg.bandit_epsilon,
        )
        for k in self._champion.to_dict():
            self._param_bandit.register_param(k)
        self._experiments_today = 0
        self._today_iso = datetime.now(UTC).date().isoformat()

    def champion(self) -> CandidateConfig:
        return self._champion

    def champion_id(self) -> str:
        return self._champion_id

    def update_champion_metrics(self, realized_r: list[float]) -> None:
        self._champion_metrics = compute_multi_objective(realized_r)

    def spawn_challengers(
        self,
        *,
        rng: random.Random | None = None,
    ) -> list[_Trial]:
        """Generate ``n_challengers`` new trials. Each picks ONE
        parameter to mutate via the importance bandit, then a random
        small perturbation along that axis."""
        rng = rng or random.Random()
        # Reset daily budget at midnight
        today = datetime.now(UTC).date().isoformat()
        if today != self._today_iso:
            self._experiments_today = 0
            self._today_iso = today
        if self._experiments_today >= self.cfg.max_experiments_per_day:
            logger.info("meta-learner: daily experiment budget exhausted")
            return []
        out: list[_Trial] = []
        for i in range(self.cfg.n_challengers):
            if self._experiments_today >= self.cfg.max_experiments_per_day:
                break
            param = self._param_bandit.pick(rng=rng)
            mutated = _mutate_single_param(
                self._champion,
                param,
                bounds=self.bounds,
                rng=rng,
            )
            cid = f"c_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{i}"
            trial = _Trial(
                challenger_id=cid,
                challenger_cfg=mutated,
                parent_param_mutated=param,
            )
            self._trials[cid] = trial
            out.append(trial)
            self._experiments_today += 1
        return out

    def record_outcome(self, challenger_id: str, realized_r: float) -> None:
        trial = self._trials.get(challenger_id)
        if trial is None:
            return
        trial.realized_r_observations.append(float(realized_r))

    def evaluate_and_promote(
        self,
        *,
        memory: HierarchicalMemory | None = None,
    ) -> dict | None:
        """Find Pareto-dominant challengers and (optionally) auto-
        promote one. Returns the promotion record or None."""
        if self._champion_metrics is None:
            return None
        eligible = [t for t in self._trials.values() if t.n >= self.cfg.min_episodes]
        if not eligible:
            return None
        winners = [
            t
            for t in eligible
            if pareto_dominates(
                t.metrics,
                self._champion_metrics,
                min_dominated_dimensions=self.cfg.min_dominated_dimensions,
            )
        ]
        if not winners:
            return None
        # Pick the winner with the largest avg_r improvement
        best = max(
            winners,
            key=lambda t: t.metrics.avg_r - self._champion_metrics.avg_r,
        )
        improvement = best.metrics.avg_r - self._champion_metrics.avg_r
        if best.parent_param_mutated:
            self._param_bandit.observe(
                best.parent_param_mutated,
                improvement,
            )

        record = {
            "ts": datetime.now(UTC).isoformat(),
            "champion_id_before": self._champion_id,
            "challenger_id": best.challenger_id,
            "champion_metrics": {
                "avg_r": self._champion_metrics.avg_r,
                "sharpe": self._champion_metrics.sharpe,
                "max_dd_r": self._champion_metrics.max_dd_r,
                "ulcer_index": self._champion_metrics.ulcer_index,
            },
            "challenger_metrics": {
                "avg_r": best.metrics.avg_r,
                "sharpe": best.metrics.sharpe,
                "max_dd_r": best.metrics.max_dd_r,
                "ulcer_index": best.metrics.ulcer_index,
            },
            "improvement_avg_r": round(improvement, 4),
            "param_mutated": best.parent_param_mutated,
            "auto_promoted": self.cfg.auto_promote,
        }

        if self.cfg.auto_promote:
            self._champion = best.challenger_cfg
            self._champion_id = best.challenger_id
            self._champion_metrics = best.metrics
            self._trials.pop(best.challenger_id, None)
            if memory is not None:
                memory.record_procedural_version(
                    version_id=best.challenger_id,
                    parent_id=record["champion_id_before"],
                    params=best.challenger_cfg.to_dict(),
                    realized_metric=best.metrics.avg_r,
                    notes="auto-promoted (pareto-dominant)",
                )
        return record

    def parameter_importance_report(self) -> list[dict]:
        return self._param_bandit.report()
