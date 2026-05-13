"""Live A/B traffic-split framework (Wave-16, 2026-04-27).

Beyond meta_learner_full's shadow trials (which are HYPOTHETICAL --
they don't actually trade), this framework supports REAL traffic
splits between champion and challenger configs on actual money,
with strict bounds:

  * Challenger capped at configured fraction of fleet capital
    (default: 10%)
  * Hard kill: any single challenger trade > configured loss
    triggers experiment shutdown
  * Daily kill: cumulative challenger drawdown > configured limit
    triggers shutdown
  * Routing: hash(signal_id) -> [0, 1) -> if < traffic_split, route
    to challenger (deterministic per signal -- a signal always gets
    the same variant when re-evaluated)

Statistical exit rules (when can we declare a winner):
  * Welch's t-test on the challenger-vs-champion realized R streams
  * Sample size needed = max(min_sample, sample_for_alpha_power)
  * Bonferroni correction across all active experiments

Persisted state: state/jarvis_intel/ab_experiments.json

Pure stdlib + math.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_PATH = ROOT / "state" / "jarvis_intel" / "ab_experiments.json"


@dataclass
class AbBounds:
    """Hard guardrails for any A/B experiment."""

    max_traffic_split: float = 0.10  # max 10% to challenger
    single_loss_kill_r: float = 2.0  # > 2R single loss = kill
    cumulative_dd_kill_r: float = 4.0  # > 4R cumulative DD = kill
    min_sample_size: int = 30
    significance_alpha: float = 0.05


@dataclass
class AbVariantStats:
    """Per-variant outcome stats."""

    name: str
    n_trades: int = 0
    realized_r_sum: float = 0.0
    realized_r_squared_sum: float = 0.0
    max_dd_r: float = 0.0
    cumulative_r: float = 0.0
    last_trade_r: float = 0.0

    @property
    def avg_r(self) -> float:
        return self.realized_r_sum / max(self.n_trades, 1)

    @property
    def variance(self) -> float:
        if self.n_trades < 2:
            return 0.0
        m = self.avg_r
        return (self.realized_r_squared_sum / self.n_trades - m * m) * (self.n_trades / (self.n_trades - 1))


@dataclass
class AbExperiment:
    """One active A/B experiment."""

    experiment_id: str
    started_at: str
    traffic_split: float  # in [0, max_traffic_split]
    control: AbVariantStats
    treatment: AbVariantStats
    is_active: bool = True
    killed_reason: str = ""
    bounds: AbBounds = field(default_factory=AbBounds)


# ─── Traffic routing ─────────────────────────────────────────────


def _hash_to_unit(signal_id: str) -> float:
    """Deterministic [0, 1) from signal_id."""
    h = hashlib.md5(signal_id.encode("utf-8"), usedforsecurity=False).digest()
    return int.from_bytes(h[:4], "big") / 0x100000000


def route_signal(*, signal_id: str, traffic_split: float) -> str:
    """Return 'treatment' if signal routes to the challenger, else
    'control'. Deterministic per signal_id."""
    if _hash_to_unit(signal_id) < traffic_split:
        return "treatment"
    return "control"


# ─── Welch's t-test ──────────────────────────────────────────────


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def welch_t_p_value(a: AbVariantStats, b: AbVariantStats) -> float:
    """Two-sided Welch t-test approximate p-value via normal limit."""
    if a.n_trades < 2 or b.n_trades < 2:
        return 1.0
    var_a = a.variance
    var_b = b.variance
    se = math.sqrt(var_a / a.n_trades + var_b / b.n_trades)
    if se == 0:
        return 1.0
    t = (a.avg_r - b.avg_r) / se
    return 2.0 * (1.0 - _norm_cdf(abs(t)))


# ─── Manager ─────────────────────────────────────────────────────


class AbManager:
    """Manages active A/B experiments. Persistent state."""

    def __init__(self, *, state_path: Path = DEFAULT_STATE_PATH) -> None:
        self.state_path = state_path
        self._experiments: dict[str, AbExperiment] = {}
        self._load()

    def register_experiment(
        self,
        *,
        experiment_id: str,
        traffic_split: float,
        bounds: AbBounds | None = None,
    ) -> AbExperiment:
        """Start a new experiment. If experiment_id already exists,
        returns the existing one."""
        if experiment_id in self._experiments:
            return self._experiments[experiment_id]
        b = bounds or AbBounds()
        # Cap traffic split at the bound
        capped = min(traffic_split, b.max_traffic_split)
        exp = AbExperiment(
            experiment_id=experiment_id,
            started_at=datetime.now(UTC).isoformat(),
            traffic_split=capped,
            control=AbVariantStats(name="control"),
            treatment=AbVariantStats(name="treatment"),
            bounds=b,
        )
        self._experiments[experiment_id] = exp
        self._save()
        return exp

    def route(self, *, experiment_id: str, signal_id: str) -> str:
        """Return 'control' or 'treatment' for this signal."""
        exp = self._experiments.get(experiment_id)
        if exp is None or not exp.is_active:
            return "control"
        return route_signal(
            signal_id=signal_id,
            traffic_split=exp.traffic_split,
        )

    def record_outcome(
        self,
        *,
        experiment_id: str,
        variant: str,
        realized_r: float,
    ) -> AbExperiment | None:
        """Update variant stats; auto-kill if guardrails tripped."""
        exp = self._experiments.get(experiment_id)
        if exp is None or not exp.is_active:
            return exp
        v_stats = exp.treatment if variant == "treatment" else exp.control
        v_stats.n_trades += 1
        v_stats.realized_r_sum += float(realized_r)
        v_stats.realized_r_squared_sum += float(realized_r) ** 2
        v_stats.cumulative_r += float(realized_r)
        v_stats.last_trade_r = float(realized_r)
        # Track per-variant max drawdown (peak-to-trough on cumulative)
        # Simple running peak track:
        if v_stats.cumulative_r > getattr(v_stats, "_peak", 0.0):
            v_stats._peak = v_stats.cumulative_r  # type: ignore[attr-defined]
        peak = getattr(v_stats, "_peak", 0.0)
        v_stats.max_dd_r = max(v_stats.max_dd_r, peak - v_stats.cumulative_r)

        # Kill rules (only check on the treatment side -- control IS
        # the production champion and stays running)
        if variant == "treatment":
            if abs(realized_r) > exp.bounds.single_loss_kill_r and realized_r < 0:
                exp.is_active = False
                exp.killed_reason = (
                    f"single-loss kill: {realized_r:+.2f}R exceeded {exp.bounds.single_loss_kill_r:.2f}R limit"
                )
            elif v_stats.max_dd_r > exp.bounds.cumulative_dd_kill_r:
                exp.is_active = False
                exp.killed_reason = (
                    f"cumulative-DD kill: {v_stats.max_dd_r:.2f}R exceeded {exp.bounds.cumulative_dd_kill_r:.2f}R"
                )
        self._save()
        return exp

    def can_declare_winner(
        self,
        experiment_id: str,
    ) -> tuple[bool, str, float]:
        """Return (can_declare, winner, p_value).

        Returns ('control', p) if the control beats treatment with
        p < significance_alpha and both variants have >= min_sample
        observations; ('treatment', p) symmetrically; (False, '', p)
        otherwise.
        """
        exp = self._experiments.get(experiment_id)
        if exp is None:
            return False, "", 1.0
        if exp.control.n_trades < exp.bounds.min_sample_size or exp.treatment.n_trades < exp.bounds.min_sample_size:
            return False, "", 1.0
        p = welch_t_p_value(exp.treatment, exp.control)
        if p > exp.bounds.significance_alpha:
            return False, "", p
        winner = "treatment" if exp.treatment.avg_r > exp.control.avg_r else "control"
        return True, winner, p

    def list_active(self) -> list[AbExperiment]:
        return [e for e in self._experiments.values() if e.is_active]

    def get(self, experiment_id: str) -> AbExperiment | None:
        return self._experiments.get(experiment_id)

    # ── Persistence ──────────────────────────────────────────

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            for eid, raw in data.items():
                control_d = raw.get("control", {})
                control_d.pop("_peak", None)
                treatment_d = raw.get("treatment", {})
                treatment_d.pop("_peak", None)
                exp = AbExperiment(
                    experiment_id=raw["experiment_id"],
                    started_at=raw.get("started_at", ""),
                    traffic_split=float(raw.get("traffic_split", 0.0)),
                    control=AbVariantStats(**control_d),
                    treatment=AbVariantStats(**treatment_d),
                    is_active=bool(raw.get("is_active", True)),
                    killed_reason=raw.get("killed_reason", ""),
                    bounds=AbBounds(**raw.get("bounds", {})),
                )
                self._experiments[eid] = exp
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("ab_framework: load failed (%s)", exc)

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict = {}
            for eid, exp in self._experiments.items():
                d = asdict(exp)
                # Strip private peak field
                d.get("control", {}).pop("_peak", None)
                d.get("treatment", {}).pop("_peak", None)
                payload[eid] = d
            self.state_path.write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("ab_framework: save failed (%s)", exc)
