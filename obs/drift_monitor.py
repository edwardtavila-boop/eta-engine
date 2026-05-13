"""
EVOLUTIONARY TRADING ALGO  //  obs.drift_monitor
==========================================
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

This module is intentionally small. It does one thing: take recent
trades + a baseline + thresholds, return a DriftAssessment. It does
NOT decide what to do with the assessment (auto-demote, page operator,
etc.) — those decisions live with the funnel/avengers layer.

Inputs
------
- ``recent``: list of completed ``Trade`` objects (from any source —
  decision_journal replay, in-memory ring buffer, broker fill log).
- ``baseline``: a ``BaselineSnapshot`` capturing the promotion-time
  win rate, expectancy, and per-trade variance. Usually loaded from
  a pinned ``BacktestResult`` saved at promotion time.
- ``min_trades`` / thresholds: per-call so different strategies can
  use different sensitivities.

Outputs
-------
- ``DriftAssessment``: severity (``green``/``amber``/``red``) plus a
  list of human-readable reasons. Designed for direct rendering into
  the decision_journal as a ``GRADER`` actor event.

Algorithm
---------
1. If ``len(recent) < min_trades``: return ``green`` with reason
   "insufficient sample". Don't false-alarm on tiny samples.
2. Compute recent win rate, mean R, sum R.
3. Compare to baseline. Win rate uses a normal-approximation
   z-score against the baseline win-rate sample variance; mean R
   uses a t-style standardization against ``baseline.r_stddev``.
4. ``red`` if either |z| >= ``red_z``; ``amber`` if either |z| >=
   ``amber_z``; otherwise ``green``.
5. Reasons enumerate the metrics that crossed each threshold.

The thresholds default to z=2.0 amber / z=3.0 red, which corresponds
to roughly a 5%/0.3% false-alarm rate per check on a stable strategy.
Tune per-strategy if needed.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Sequence

    from eta_engine.backtest.models import Trade


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
    def from_trades(cls, strategy_id: str, trades: Sequence[Trade]) -> BaselineSnapshot:
        if not trades:
            return cls(strategy_id=strategy_id, n_trades=0, win_rate=0.0, avg_r=0.0, r_stddev=0.0)
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
    on the first few live fills. Increase ``min_trades`` if alerts are
    too noisy; decrease for fast-trading strategies where 20 fills
    arrive within an hour.
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
        reasons.append(f"win rate {recent_wr * 100:.1f}% vs baseline {p0 * 100:.1f}% (z={wr_z:+.2f}, |z|>={red_z})")
    elif abs(wr_z) >= amber_z:
        severity = "amber"
        reasons.append(f"win rate {recent_wr * 100:.1f}% vs baseline {p0 * 100:.1f}% (z={wr_z:+.2f}, |z|>={amber_z})")

    if abs(r_z) >= red_z:
        severity = "red"
        reasons.append(f"avg R {recent_mean:+.3f} vs baseline {baseline.avg_r:+.3f} (z={r_z:+.2f}, |z|>={red_z})")
    elif abs(r_z) >= amber_z and severity == "green":
        severity = "amber"
        reasons.append(f"avg R {recent_mean:+.3f} vs baseline {baseline.avg_r:+.3f} (z={r_z:+.2f}, |z|>={amber_z})")
    elif abs(r_z) >= amber_z:
        # Already amber/red from win rate; append the r-side reason
        # so the operator sees both.
        reasons.append(f"avg R {recent_mean:+.3f} vs baseline {baseline.avg_r:+.3f} (z={r_z:+.2f})")

    if severity == "green" and not reasons:
        reasons.append(f"within {amber_z}σ of baseline (wr_z={wr_z:+.2f}, r_z={r_z:+.2f})")

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


# ---------------------------------------------------------------------------
# Fleet correlation penalty (quant-sage 2026-04-27)
# ---------------------------------------------------------------------------
#
# Quant-sage flagged that BTC + ETH on the same `crypto_orb` may be
# one strategy not two — DSR for the fleet needs an explicit
# correlation penalty, otherwise we're double-counting the same edge.
# The :class:`FleetCorrelationAssessment` quantifies that risk on
# realized trade R-values so the operator sees when two bots
# tagged via ``extras["fleet_corr_partner"]`` are firing in lockstep.
#
# Severity escalates as correlation rises:
#   * < amber_rho            -> green   (independent enough)
#   * amber_rho..red_rho     -> amber   (treat as 1.5x exposure)
#   * >= red_rho             -> red     (treat as ONE bot for risk)


class FleetCorrelationAssessment(BaseModel):
    """Quant-sage's correlation-penalty verdict for a registered pair.

    ``recommended_action`` is the operator-facing instruction:
      * ``"independent"`` — risk-budget the two bots normally.
      * ``"halve_one"`` — keep both bots active but cut their per-bot
        risk in half so the sum mimics a single-bot exposure.
      * ``"merge_for_risk"`` — treat the pair as ONE bot for the
        FleetRiskGate / portfolio-rebalancer; the realized PnL already
        looks single-source, so per-bot caps do nothing.
    """

    bot_a: str
    bot_b: str
    severity: Severity
    n_paired: int = Field(ge=0)
    rho: float = Field(ge=-1.0, le=1.0)
    recommended_action: Literal["independent", "halve_one", "merge_for_risk"]
    reasons: list[str] = Field(default_factory=list)


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation; 0.0 on degenerate inputs."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    sxx = sum((xs[i] - mx) ** 2 for i in range(n))
    syy = sum((ys[i] - my) ** 2 for i in range(n))
    denom = math.sqrt(sxx * syy)
    if denom <= 0.0:
        return 0.0
    return sxy / denom


def assess_fleet_correlation(
    *,
    bot_a: str,
    recent_a: Sequence[Trade],
    bot_b: str,
    recent_b: Sequence[Trade],
    min_paired: int = 10,
    amber_rho: float = 0.5,
    red_rho: float = 0.7,
) -> FleetCorrelationAssessment:
    """Score realised-R correlation between a registered bot pair.

    The pair's recent trade streams are zipped by sequence position
    (the n-th trade of each), so this is a *trade-aligned* Pearson
    rho — not a calendar-aligned one. That's intentional: bots fire
    asynchronously, but two bots monetising the same regime tend to
    open trades within minutes of each other, so sequence alignment
    is a reasonable proxy with much less plumbing than calendar
    alignment.

    Sample sizes below ``min_paired`` return ``green`` with a
    "insufficient sample" reason — same idiom as ``assess_drift``.

    Thresholds default to amber=0.5 / red=0.7, matching the registry
    rationale on the eth_perp + btc_hybrid pair: "if rho > 0.7 over
    30 days, treat the pair as one bot for risk-budget purposes."
    """
    rs_a = [t.pnl_r for t in recent_a]
    rs_b = [t.pnl_r for t in recent_b]
    n = min(len(rs_a), len(rs_b))
    if n < min_paired:
        return FleetCorrelationAssessment(
            bot_a=bot_a,
            bot_b=bot_b,
            severity="green",
            n_paired=n,
            rho=0.0,
            recommended_action="independent",
            reasons=[f"insufficient sample: {n} < {min_paired} paired trades"],
        )

    xs = rs_a[-n:]
    ys = rs_b[-n:]
    rho = _pearson(xs, ys)

    if rho >= red_rho:
        return FleetCorrelationAssessment(
            bot_a=bot_a,
            bot_b=bot_b,
            severity="red",
            n_paired=n,
            rho=rho,
            recommended_action="merge_for_risk",
            reasons=[
                f"trade-aligned rho={rho:+.2f} >= {red_rho} over {n} trades; "
                "the pair monetises the same edge — treat as ONE bot for "
                "fleet risk-budget purposes."
            ],
        )
    if rho >= amber_rho:
        return FleetCorrelationAssessment(
            bot_a=bot_a,
            bot_b=bot_b,
            severity="amber",
            n_paired=n,
            rho=rho,
            recommended_action="halve_one",
            reasons=[
                f"trade-aligned rho={rho:+.2f} in [{amber_rho},{red_rho}) "
                f"over {n} trades; cut one bot's risk_per_trade_pct in "
                "half so the pair's combined exposure mimics a single bot."
            ],
        )
    return FleetCorrelationAssessment(
        bot_a=bot_a,
        bot_b=bot_b,
        severity="green",
        n_paired=n,
        rho=rho,
        recommended_action="independent",
        reasons=[f"trade-aligned rho={rho:+.2f} < {amber_rho} over {n} trades"],
    )
