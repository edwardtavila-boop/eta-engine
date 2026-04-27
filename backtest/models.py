"""
EVOLUTIONARY TRADING ALGO  //  backtest.models
==================================
Pydantic v2 models for backtest trades, results, and config.
"""

from __future__ import annotations

import datetime as _datetime_runtime  # noqa: F401  -- pydantic v2 forward-ref resolution
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
    """A single completed trade from a backtest run."""

    entry_time: datetime
    exit_time: datetime
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: float = Field(gt=0.0)
    entry_price: float = Field(gt=0.0)
    exit_price: float = Field(gt=0.0)
    pnl_r: float = Field(description="PnL in R multiples (pos=win, neg=loss)")
    pnl_usd: float = Field(description="Realized PnL in USD")
    confluence_score: float = Field(ge=0.0, le=10.0)
    leverage_used: float = Field(ge=0.0)
    max_drawdown_during: float = Field(
        ge=0.0,
        description="Peak unrealized drawdown (USD) during trade life",
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
