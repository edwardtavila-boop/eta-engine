"""
EVOLUTIONARY TRADING ALGO  //  backtest.metrics
===================================
Pure-math performance stats. No pandas, no numpy required.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from eta_engine.backtest.models import Trade


def _mean(xs: Iterable[float]) -> float:
    xs = list(xs)
    if not xs:
        return 0.0
    return sum(xs) / len(xs)


def _stdev(xs: Iterable[float]) -> float:
    xs = list(xs)
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def compute_sharpe(returns: list[float], risk_free: float = 0.0) -> float:
    """Annualized Sharpe ratio assuming 252 trading days.

    Returns 0.0 if stdev is zero or sample too small.
    """
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free for r in returns]
    mu = _mean(excess)
    sd = _stdev(excess)
    if sd == 0.0:
        return 0.0
    return round(mu / sd * math.sqrt(252), 4)


def compute_sortino(returns: list[float]) -> float:
    """Annualized Sortino ratio — downside deviation only.

    When no downside samples exist, falls back to the return stdev so the ratio
    stays finite and order-of-magnitude-comparable with Sharpe.
    """
    if len(returns) < 2:
        return 0.0
    downside = [r for r in returns if r < 0.0]
    dd = _stdev(downside) if downside else _stdev(returns)
    if dd == 0.0:
        return 0.0
    return round(_mean(returns) / dd * math.sqrt(252), 4)


def compute_profit_factor(trades: list[Trade]) -> float:
    """Sum of winning PnL / abs(sum of losing PnL)."""
    gross_win = sum(t.pnl_usd for t in trades if t.pnl_usd > 0.0)
    gross_loss = sum(-t.pnl_usd for t in trades if t.pnl_usd < 0.0)
    if gross_loss == 0.0:
        return 0.0 if gross_win == 0.0 else float("inf")
    return round(gross_win / gross_loss, 4)


def compute_max_dd(equity_curve: list[float]) -> float:
    """Maximum drawdown as positive percentage (0–100) from running peak."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        if peak > 0.0:
            dd = (peak - v) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return round(max_dd, 4)


def compute_expectancy(trades: list[Trade]) -> float:
    """Per-trade expectancy in R multiples."""
    if not trades:
        return 0.0
    wins = [t.pnl_r for t in trades if t.pnl_r > 0.0]
    losses = [t.pnl_r for t in trades if t.pnl_r <= 0.0]
    n = len(trades)
    win_rate = len(wins) / n
    avg_win = _mean(wins) if wins else 0.0
    avg_loss = abs(_mean(losses)) if losses else 0.0
    return round(win_rate * avg_win - (1.0 - win_rate) * avg_loss, 4)
