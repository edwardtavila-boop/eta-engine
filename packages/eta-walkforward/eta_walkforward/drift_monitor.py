"""
eta_walkforward.drift_monitor
=============================
Live-vs-baseline drift detection for a strategy's recent trades.

Why this exists
---------------
A strategy that passed the walk-forward gate at promotion time can
still decay in production. Regimes shift, slippage creeps, broker
behaviour changes, an upstream feature breaks silently. The first
warning is usually a stretch of bad PnL — but by then you've already
bled. A drift monitor surfaces the divergence *before* it shows up
in equity by comparing recent live trade metrics against the
strategy's promoted baseline.

This module does one thing: take recent trades + a baseline +
thresholds, return a ``DriftAssessment``. It does NOT decide what
to do with the assessment (auto-demote, page operator, etc.).

Inputs
------
- ``recent``: list of completed ``Trade`` objects from any source
  (decision_journal replay, in-memory ring buffer, broker fill log).
- ``baseline``: ``BaselineSnapshot`` capturing promotion-time
  win rate, expectancy, and per-trade variance.
- ``min_trades`` / thresholds: per-call so different strategies can
  use different sensitivities.

Outputs
-------
``DriftAssessment``: severity (``green``/``amber``/``red``) plus a
list of human-readable reasons.

Algorithm
---------
1. If ``len(recent) < min_trades``: return ``green`` with reason
   "insufficient sample". Don't false-alarm on tiny samples.
2. Compute recent win rate and mean R.
3. Compare to baseline. Win rate uses a normal-approximation
   z-score against the baseline win-rate sample variance; mean R
   uses a t-style standardization against ``baseline.r_stddev``.
4. ``red`` if either |z| >= ``red_z``; ``amber`` if either |z| >=
   ``amber_z``; otherwise ``green``.
5. Reasons enumerate the metrics that crossed each threshold.

Default thresholds (z=2.0 amber / z=3.0 red) correspond to roughly
5% / 0.3% false-alarm per check on a stable strategy.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Sequence

    from eta_walkforward.models import Trade


Severity = Literal["green", "amber", "red"]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class BaselineSnapshot(BaseModel):
    """Promotion-time stats a strategy is monitored against."""

    strategy_id: str
    n_trades: int = Field(ge=0)
    win_rate: float = Field(ge=0.0, le=1.0)
    avg_r: float = Field(description="Mean per-trade R (can be negative)")
    r_stddev: float = Field(ge=0.0, description="Per-trade R standard deviation")

    @classmethod
    def from_trades(
        cls, strategy_id: str, trades: Sequence[Trade],
    ) -> BaselineSnapshot:
        if not trades:
            return cls(
                strategy_id=strategy_id, n_trades=0,
                win_rate=0.0, avg_r=0.0, r_stddev=0.0,
            )
        rs = [t.pnl_r for t in trades]
        n = len(rs)
        mean = sum(rs) / n
        wins = sum(1 for r in rs if r > 0.0)
        if n > 1:
            var = sum((r - mean) ** 2 for r in rs) / (n - 1)
            sd = math.sqrt(var)
        else:
            sd = 0.0
        return cls(
            strategy_id=strategy_id,
            n_trades=n,
            win_rate=wins / n,
            avg_r=mean,
            r_stddev=sd,
        )


class DriftAssessment(BaseModel):
    """Output of a drift check against a baseline."""

    strategy_id: str
    severity: Severity
    n_recent: int = Field(ge=0)
    recent_win_rate: float = Field(ge=0.0, le=1.0)
    recent_avg_r: float
    win_rate_z: float = Field(description="Z-score of recent vs baseline win rate")
    avg_r_z: float = Field(description="Z-score of recent vs baseline avg R")
    reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------


def assess_drift(
    *,
    strategy_id: str,
    recent: Sequence[Trade],
    baseline: BaselineSnapshot,
    min_trades: int = 20,
    amber_z: float = 2.0,
    red_z: float = 3.0,
) -> DriftAssessment:
    """Compute a drift assessment of ``recent`` vs ``baseline``.

    The output severity is ``green`` whenever the sample is too small
    to assess (``len(recent) < min_trades``) — this prevents flapping
    on the first few live fills. Increase ``min_trades`` if alerts
    are too noisy; decrease for fast-trading strategies where 20
    fills arrive within an hour.
    """
    n = len(recent)
    if n < min_trades:
        return DriftAssessment(
            strategy_id=strategy_id,
            severity="green",
            n_recent=n,
            recent_win_rate=0.0,
            recent_avg_r=0.0,
            win_rate_z=0.0,
            avg_r_z=0.0,
            reasons=[f"insufficient sample: {n} < {min_trades} trades"],
        )

    rs = [t.pnl_r for t in recent]
    recent_mean = sum(rs) / n
    recent_wins = sum(1 for r in rs if r > 0.0)
    recent_wr = recent_wins / n

    # Win-rate z under H0 = baseline.win_rate, sample of size n.
    p0 = baseline.win_rate
    if 0.0 < p0 < 1.0:
        wr_se = math.sqrt(p0 * (1.0 - p0) / n)
        wr_z = (recent_wr - p0) / wr_se if wr_se > 0.0 else 0.0
    else:
        # Degenerate baseline (always-win or always-loss): can't z-score.
        # Fall back to absolute delta in WR-percentage-points expressed
        # as a pseudo-z so the same thresholds still trigger.
        wr_z = (recent_wr - p0) * 10.0

    # Avg-R z against baseline.r_stddev / sqrt(n).
    if baseline.r_stddev > 0.0:
        r_se = baseline.r_stddev / math.sqrt(n)
        r_z = (recent_mean - baseline.avg_r) / r_se if r_se > 0.0 else 0.0
    else:
        r_z = (recent_mean - baseline.avg_r) * 10.0

    reasons: list[str] = []
    severity: Severity = "green"

    if abs(wr_z) >= red_z:
        severity = "red"
        reasons.append(
            f"win rate {recent_wr * 100:.1f}% vs baseline {p0 * 100:.1f}% "
            f"(z={wr_z:+.2f}, |z|>={red_z})",
        )
    elif abs(wr_z) >= amber_z:
        severity = "amber"
        reasons.append(
            f"win rate {recent_wr * 100:.1f}% vs baseline {p0 * 100:.1f}% "
            f"(z={wr_z:+.2f}, |z|>={amber_z})",
        )

    if abs(r_z) >= red_z:
        severity = "red"
        reasons.append(
            f"avg R {recent_mean:+.3f} vs baseline {baseline.avg_r:+.3f} "
            f"(z={r_z:+.2f}, |z|>={red_z})",
        )
    elif abs(r_z) >= amber_z and severity == "green":
        severity = "amber"
        reasons.append(
            f"avg R {recent_mean:+.3f} vs baseline {baseline.avg_r:+.3f} "
            f"(z={r_z:+.2f}, |z|>={amber_z})",
        )
    elif abs(r_z) >= amber_z:
        # Already amber/red from win rate; append the r-side reason
        # so the operator sees both.
        reasons.append(
            f"avg R {recent_mean:+.3f} vs baseline {baseline.avg_r:+.3f} "
            f"(z={r_z:+.2f})",
        )

    if severity == "green" and not reasons:
        reasons.append(
            f"within {amber_z}sigma of baseline "
            f"(wr_z={wr_z:+.2f}, r_z={r_z:+.2f})",
        )

    return DriftAssessment(
        strategy_id=strategy_id,
        severity=severity,
        n_recent=n,
        recent_win_rate=recent_wr,
        recent_avg_r=recent_mean,
        win_rate_z=wr_z,
        avg_r_z=r_z,
        reasons=reasons,
    )
