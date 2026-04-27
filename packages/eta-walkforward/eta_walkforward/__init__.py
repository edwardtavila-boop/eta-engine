"""
eta-walkforward
===============
Honest walk-forward strategy evaluation: strict gate, FP-noise guards,
drift monitoring.

Public API
----------
* ``evaluate_gate(cfg, windows) -> WalkForwardResult`` — the main gate.
* ``WalkForwardConfig`` — gate knobs (strict / long-haul / grid modes).
* ``WindowStats`` — per-window stats your engine produces.
* ``WalkForwardResult`` — aggregate verdict.

* ``compute_sharpe`` — Sharpe with FP-noise + deterministic-R guards.
* ``compute_sortino`` / ``compute_profit_factor`` / ``compute_max_dd`` /
  ``compute_expectancy`` — companion stats.
* ``compute_dsr`` / ``compute_probabilistic_sharpe`` — Bailey & López
  de Prado deflated-Sharpe formulas.

* ``BaselineSnapshot`` — promotion-time stats a strategy is monitored
  against.
* ``DriftAssessment`` — output of ``assess_drift``.
* ``assess_drift(strategy_id, recent, baseline, ...)`` — z-score drift
  detection on recent vs baseline trades.

* ``Trade`` / ``BacktestResult`` / ``BacktestConfig`` — minimal models
  the API consumes.
"""

from eta_walkforward.deflated_sharpe import (
    compute_dsr,
    compute_probabilistic_sharpe,
)
from eta_walkforward.drift_monitor import (
    BaselineSnapshot,
    DriftAssessment,
    Severity,
    assess_drift,
)
from eta_walkforward.metrics import (
    compute_expectancy,
    compute_max_dd,
    compute_profit_factor,
    compute_sharpe,
    compute_sortino,
)
from eta_walkforward.models import BacktestConfig, BacktestResult, Trade
from eta_walkforward.walk_forward import (
    WalkForwardConfig,
    WalkForwardResult,
    WindowStats,
    evaluate_gate,
)

__version__ = "0.1.0"

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "BaselineSnapshot",
    "DriftAssessment",
    "Severity",
    "Trade",
    "WalkForwardConfig",
    "WalkForwardResult",
    "WindowStats",
    "__version__",
    "assess_drift",
    "compute_dsr",
    "compute_expectancy",
    "compute_max_dd",
    "compute_probabilistic_sharpe",
    "compute_profit_factor",
    "compute_sharpe",
    "compute_sortino",
    "evaluate_gate",
]
