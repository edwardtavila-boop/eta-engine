"""Performance metrics roll-up + DSR (Tier-2 #8 + #20, 2026-04-27).

Computes the standard risk-adjusted return metrics from an equity
curve or returns series:

  * Sharpe ratio (annualized)
  * Sortino ratio (downside-only deviation)
  * Calmar ratio (return / max DD)
  * Probabilistic Sharpe Ratio (PSR / DSR)
  * Profit factor
  * Win rate
  * Expectancy (mean R per trade)

The Probabilistic Sharpe Ratio (Bailey & Lopez de Prado 2014) is the
backbone of the overfitting-detection here: it answers "given the
length of the track record AND the higher moments (skew + kurt) of
returns, what's the probability that the TRUE Sharpe is above a
benchmark?"

Operator runs ``python scripts/eta_perf_report.py`` daily / weekly to
inspect the rolling metrics and catch degradation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PerfMetrics:
    n_trades: int
    win_rate: float
    profit_factor: float
    expectancy_r: float
    sharpe: float
    sortino: float
    calmar: float
    max_dd_pct: float
    psr: float  # P[Sharpe > 0]
    psr_vs_one: float  # P[Sharpe > 1.0]
    skew: float
    kurtosis: float


def _moments(xs: list[float]) -> tuple[float, float, float, float]:
    n = len(xs)
    if n == 0:
        return 0.0, 0.0, 0.0, 0.0
    mean = sum(xs) / n
    if n < 2:
        return mean, 0.0, 0.0, 0.0
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return mean, 0.0, 0.0, 0.0
    skew = sum((x - mean) ** 3 for x in xs) / (n * (sd**3))
    kurt = sum((x - mean) ** 4 for x in xs) / (n * (sd**4)) - 3.0  # excess
    return mean, sd, skew, kurt


def _max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _norm_cdf(z: float) -> float:
    """Standard normal CDF approximation."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def probabilistic_sharpe_ratio(
    returns: list[float],
    *,
    target_sharpe: float = 0.0,
    ann_factor: float = 252.0,
) -> float:
    """PSR = P(true_Sharpe > target | observed_Sharpe, n, skew, kurt).

    From Bailey & Lopez de Prado (2014). Higher moments matter:
    the same point-estimate Sharpe is less reliable if returns are
    negatively skewed or fat-tailed.
    """
    n = len(returns)
    if n < 5:
        return 0.0
    mean, sd, skew, kurt = _moments(returns)
    if sd == 0:
        return 1.0 if mean >= target_sharpe / math.sqrt(ann_factor) else 0.0
    sharpe_per_step = mean / sd
    target_per_step = target_sharpe / math.sqrt(ann_factor)
    # Stationary distribution of Sharpe ratio under PSR derivation
    se = math.sqrt((1.0 - skew * sharpe_per_step + (kurt + 2) / 4 * (sharpe_per_step**2)) / (n - 1))
    z = (sharpe_per_step - target_per_step) / se
    return round(_norm_cdf(z), 4)


def compute_metrics(
    *,
    r_multiples: list[float],
    equity_curve: list[float] | None = None,
    ann_factor: float = 252.0,
) -> PerfMetrics:
    """Build the full PerfMetrics from a list of per-trade R-multiples.

    ``equity_curve`` is optional; when present, used for max-DD. When
    None, equity is reconstructed by cumulative sum of R-multiples
    (assumes 1R risk per trade, equity starts at 0).
    """
    n = len(r_multiples)
    if n == 0:
        return PerfMetrics(
            n_trades=0,
            win_rate=0.0,
            profit_factor=0.0,
            expectancy_r=0.0,
            sharpe=0.0,
            sortino=0.0,
            calmar=0.0,
            max_dd_pct=0.0,
            psr=0.0,
            psr_vs_one=0.0,
            skew=0.0,
            kurtosis=0.0,
        )

    wins = [r for r in r_multiples if r > 0]
    losses = [r for r in r_multiples if r < 0]
    win_rate = len(wins) / n
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")

    mean, sd, skew, kurt = _moments(r_multiples)

    # Sortino: penalize only downside deviation
    downside = [r for r in r_multiples if r < 0]
    if downside:
        d_mean, d_sd, _, _ = _moments(downside)
    else:
        d_sd = 0.0

    sharpe = (mean / sd * math.sqrt(ann_factor)) if sd > 0 else 0.0
    sortino = (mean / d_sd * math.sqrt(ann_factor)) if d_sd > 0 else (float("inf") if mean > 0 else 0.0)

    # Equity curve: use provided or reconstruct
    if equity_curve is None:
        eq = [0.0]
        running = 0.0
        for r in r_multiples:
            running += r
            eq.append(running)
        # Shift to start at 100 for ratio computation
        eq = [100.0 + e for e in eq]
    else:
        eq = equity_curve

    max_dd = _max_drawdown(eq)
    final = eq[-1] if eq else 0.0
    start = eq[0] if eq else 1.0
    total_return = (final / start - 1.0) if start else 0.0
    calmar = (total_return / max_dd) if max_dd > 0 else 0.0

    return PerfMetrics(
        n_trades=n,
        win_rate=round(win_rate, 4),
        profit_factor=round(profit_factor, 4) if profit_factor != float("inf") else 999.0,
        expectancy_r=round(mean, 4),
        sharpe=round(sharpe, 4),
        sortino=round(sortino, 4) if sortino != float("inf") else 999.0,
        calmar=round(calmar, 4),
        max_dd_pct=round(max_dd, 4),
        psr=probabilistic_sharpe_ratio(r_multiples, target_sharpe=0.0, ann_factor=ann_factor),
        psr_vs_one=probabilistic_sharpe_ratio(r_multiples, target_sharpe=1.0, ann_factor=ann_factor),
        skew=round(skew, 4),
        kurtosis=round(kurt, 4),
    )
