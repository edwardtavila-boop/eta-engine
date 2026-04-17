"""
EVOLUTIONARY TRADING ALGO  //  backtest.walk_forward
========================================
Real walk-forward analysis. Rolling OR anchored IS/OOS splits with DSR gate.

Gate (all three must hold):
  * DSR > 0.5
  * OOS degradation < 35%
  * min_trades_per_window satisfied in every window
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field

from eta_engine.backtest.deflated_sharpe import compute_dsr
from eta_engine.backtest.engine import BacktestEngine
from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.features.pipeline import FeaturePipeline

# ---------------------------------------------------------------------------
# Config / result models
# ---------------------------------------------------------------------------

class WalkForwardConfig(BaseModel):
    window_days: int = Field(ge=1)
    step_days: int = Field(ge=1)
    anchored: bool = False
    oos_fraction: float = Field(default=0.3, gt=0.0, lt=1.0)
    min_trades_per_window: int = Field(default=20, ge=1)


class WalkForwardResult(BaseModel):
    windows: list[dict] = Field(default_factory=list)
    aggregate_is_sharpe: float = 0.0
    aggregate_oos_sharpe: float = 0.0
    oos_degradation_avg: float = 0.0
    deflated_sharpe: float = 0.0
    pass_gate: bool = False


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class WalkForwardEngine:
    """Splits bars into IS/OOS windows and runs a fresh backtest per split."""

    def run(
        self,
        bars: list[BarData],
        pipeline: FeaturePipeline,
        config: WalkForwardConfig,
        base_backtest_config: BacktestConfig,
        ctx_builder: Any | None = None,
    ) -> WalkForwardResult:
        if not bars:
            return WalkForwardResult()
        windows = self._build_windows(bars, config)
        win_results: list[dict] = []
        for i, (is_start, is_end, oos_end) in enumerate(windows):
            is_bars = [b for b in bars if is_start <= b.timestamp < is_end]
            oos_bars = [b for b in bars if is_end <= b.timestamp < oos_end]
            if not is_bars or not oos_bars:
                continue
            is_cfg = base_backtest_config.model_copy(update={
                "start_date": is_bars[0].timestamp,
                "end_date": is_bars[-1].timestamp,
            })
            oos_cfg = base_backtest_config.model_copy(update={
                "start_date": oos_bars[0].timestamp,
                "end_date": oos_bars[-1].timestamp,
            })
            is_res = BacktestEngine(pipeline, is_cfg, ctx_builder=ctx_builder,
                                    strategy_id=f"wf-{i}-IS").run(is_bars)
            oos_res = BacktestEngine(pipeline, oos_cfg, ctx_builder=ctx_builder,
                                     strategy_id=f"wf-{i}-OOS").run(oos_bars)
            win_results.append({
                "window": i,
                "is_start": is_start.isoformat(),
                "is_end": is_end.isoformat(),
                "oos_end": oos_end.isoformat(),
                "is_sharpe": is_res.sharpe,
                "oos_sharpe": oos_res.sharpe,
                "is_trades": is_res.n_trades,
                "oos_trades": oos_res.n_trades,
                "is_return_pct": is_res.total_return_pct,
                "oos_return_pct": oos_res.total_return_pct,
                "degradation_pct": _degradation(is_res.sharpe, oos_res.sharpe),
                "min_trades_met": (
                    is_res.n_trades >= config.min_trades_per_window
                    and oos_res.n_trades >= config.min_trades_per_window
                ),
            })
        return self._aggregate(win_results, config)

    # ------------------------------------------------------------------
    # Window builder
    # ------------------------------------------------------------------
    @staticmethod
    def _build_windows(
        bars: list[BarData],
        cfg: WalkForwardConfig,
    ) -> list[tuple[Any, Any, Any]]:
        if not bars:
            return []
        start = bars[0].timestamp
        end = bars[-1].timestamp
        window = timedelta(days=cfg.window_days)
        step = timedelta(days=cfg.step_days)
        oos_len = window * cfg.oos_fraction
        is_len = window - oos_len
        windows: list[tuple[Any, Any, Any]] = []
        cursor_is_start = start
        while True:
            is_end = cursor_is_start + is_len
            oos_end = is_end + oos_len
            if oos_end > end:
                break
            effective_is_start = start if cfg.anchored else cursor_is_start
            windows.append((effective_is_start, is_end, oos_end))
            cursor_is_start = cursor_is_start + step
        return windows

    # ------------------------------------------------------------------
    # Aggregation + DSR gate
    # ------------------------------------------------------------------
    def _aggregate(
        self,
        wins: list[dict],
        cfg: WalkForwardConfig,
    ) -> WalkForwardResult:
        if not wins:
            return WalkForwardResult()
        is_sharpes = [w["is_sharpe"] for w in wins]
        oos_sharpes = [w["oos_sharpe"] for w in wins]
        agg_is = sum(is_sharpes) / len(is_sharpes)
        agg_oos = sum(oos_sharpes) / len(oos_sharpes)
        degradations = [w["degradation_pct"] for w in wins]
        deg_avg = sum(degradations) / len(degradations) if degradations else 0.0
        # Estimate skew/kurtosis of the OOS sharpe distribution across windows
        skew, kurt = _moments(oos_sharpes)
        dsr = compute_dsr(
            sharpe=agg_oos,
            n_trades=max(sum(w["oos_trades"] for w in wins), 2),
            skew=skew,
            kurtosis=kurt,
            n_trials=len(wins),
        )
        all_met = all(w["min_trades_met"] for w in wins)
        gate = dsr > 0.5 and deg_avg < 0.35 and all_met
        return WalkForwardResult(
            windows=wins,
            aggregate_is_sharpe=round(agg_is, 4),
            aggregate_oos_sharpe=round(agg_oos, 4),
            oos_degradation_avg=round(deg_avg, 4),
            deflated_sharpe=round(dsr, 4),
            pass_gate=gate,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _degradation(is_sharpe: float, oos_sharpe: float) -> float:
    if is_sharpe <= 0.0:
        return 0.0 if oos_sharpe >= is_sharpe else 1.0
    return round(max((is_sharpe - oos_sharpe) / is_sharpe, 0.0), 4)


def _moments(xs: list[float]) -> tuple[float, float]:
    n = len(xs)
    if n < 2:
        return 0.0, 3.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    if var <= 0.0:
        return 0.0, 3.0
    sd = math.sqrt(var)
    skew = sum((x - m) ** 3 for x in xs) / (n * sd ** 3)
    kurt = sum((x - m) ** 4 for x in xs) / (n * sd ** 4)
    return skew, kurt
