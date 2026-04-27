"""
eta_walkforward.models
======================
Pydantic v2 models for trades, results, and backtest config.

The minimal subset needed by the walk-forward gate, drift monitor,
and engine. Strategies and ctx-builders are deliberately not in
this package — those live in your own code.
"""

from __future__ import annotations

import datetime as _datetime_runtime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from datetime import datetime
else:
    datetime = _datetime_runtime.datetime


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------


class Trade(BaseModel):
    """A single completed trade from a backtest run.

    The ``pnl_r`` and ``pnl_usd`` fields are sign-correct for both
    long and short trades — positive = win, negative = loss
    regardless of side. Implementers compute these from
    (exit_price - entry_price) * qty * side_sign in their engine
    and store the result here.
    """

    entry_time: datetime
    exit_time: datetime
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: float = Field(gt=0.0)
    entry_price: float = Field(gt=0.0)
    exit_price: float = Field(gt=0.0)
    pnl_r: float = Field(description="PnL in R multiples (pos=win, neg=loss)")
    pnl_usd: float = Field(description="Realized PnL in USD")
    confluence_score: float = Field(default=0.0, ge=0.0, le=10.0)
    leverage_used: float = Field(default=1.0, ge=0.0)
    max_drawdown_during: float = Field(
        default=0.0,
        ge=0.0,
        description="Peak unrealized drawdown (USD) during trade life",
    )
    regime: str | None = Field(
        default=None,
        description=(
            "Regime label active at trade entry. Optional — populated "
            "by your regime classifier if you have one."
        ),
    )
    exit_reason: str | None = Field(
        default=None,
        description=(
            "Why the trade closed: 'target_hit', 'stop_hit', 'trail_stop', "
            "'time_stop', 'session_close', 'kill_switch', etc."
        ),
    )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


class BacktestResult(BaseModel):
    """Aggregate output of a single backtest run."""

    strategy_id: str
    n_trades: int = Field(ge=0)
    win_rate: float = Field(ge=0.0, le=1.0)
    avg_win_r: float = Field(ge=0.0)
    avg_loss_r: float = Field(ge=0.0, description="Magnitude of avg loss (positive)")
    expectancy_r: float = Field(description="Per-trade expectancy in R")
    profit_factor: float = Field(ge=0.0)
    sharpe: float
    sortino: float
    max_dd_pct: float = Field(ge=0.0, le=100.0)
    total_return_pct: float
    trades: list[Trade] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class BacktestConfig(BaseModel):
    """Configuration for a single backtest run."""

    start_date: datetime
    end_date: datetime
    symbol: str
    initial_equity: float = Field(gt=0.0)
    risk_per_trade_pct: float = Field(gt=0.0, le=0.10)
    confluence_threshold: float = Field(default=7.0, ge=0.0, le=10.0)
    max_trades_per_day: int = Field(default=5, ge=1)
    stop_r_multiple: float = Field(default=2.0, gt=0.0)
    target_r_multiple: float = Field(default=3.0, gt=0.0)
    atr_stop_mult: float = Field(default=2.0, gt=0.0)


# ---------------------------------------------------------------------------
# Forward-ref resolution
# ---------------------------------------------------------------------------
# `from __future__ import annotations` makes every annotation a string;
# pydantic v2 resolves these lazily on first model_validate() call.
# Calling model_rebuild() at import time pins the resolution.

Trade.model_rebuild()
BacktestResult.model_rebuild()
BacktestConfig.model_rebuild()
