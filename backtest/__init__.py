"""
EVOLUTIONARY TRADING ALGO  //  backtest
===========================
Bar-replay backtest harness, metrics, tearsheet rendering.
"""

from eta_engine.backtest.deflated_sharpe import (
    compute_dsr,
    compute_probabilistic_sharpe,
)
from eta_engine.backtest.engine import BacktestEngine
from eta_engine.backtest.metrics import (
    compute_expectancy,
    compute_max_dd,
    compute_profit_factor,
    compute_sharpe,
    compute_sortino,
)
from eta_engine.backtest.models import BacktestConfig, BacktestResult, Trade
from eta_engine.backtest.replay import BarReplay
from eta_engine.backtest.tearsheet import TearsheetBuilder
from eta_engine.backtest.walk_forward import (
    WalkForwardConfig,
    WalkForwardEngine,
    WalkForwardResult,
)

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "BarReplay",
    "TearsheetBuilder",
    "Trade",
    "WalkForwardConfig",
    "WalkForwardEngine",
    "WalkForwardResult",
    "compute_dsr",
    "compute_expectancy",
    "compute_max_dd",
    "compute_probabilistic_sharpe",
    "compute_profit_factor",
    "compute_sharpe",
    "compute_sortino",
]
