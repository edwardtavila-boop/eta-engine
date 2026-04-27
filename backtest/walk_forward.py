"""
EVOLUTIONARY TRADING ALGO  //  backtest.walk_forward
========================================
Real walk-forward analysis. Rolling OR anchored IS/OOS splits with DSR gate.

Gate layers (all layers must hold for the legacy gate; strict mode adds more):
  * Aggregate DSR > 0.5               -- single DSR on the mean OOS Sharpe
  * OOS degradation < 35%              -- avg IS->OOS sharpe drop
  * min_trades_per_window satisfied    -- coverage floor

Per-fold DSR (added 2026-04-24)
-------------------------------
The aggregate DSR can be lifted by a handful of outlier folds. This module
also computes a DSR *per fold*, using that fold's own OOS trade-return
distribution moments and ``n_folds`` as the trial count. We expose:

  * ``windows[i]['oos_dsr']`` / ``oos_skew`` / ``oos_kurt``
  * ``WalkForwardResult.per_fold_dsr``
  * ``WalkForwardResult.fold_dsr_median``
  * ``WalkForwardResult.fold_dsr_pass_fraction`` (fraction with DSR > 0.5)

Setting ``WalkForwardConfig.strict_fold_dsr_gate = True`` makes the gate
additionally require ``fold_dsr_median > 0.5`` AND
``fold_dsr_pass_fraction >= fold_dsr_min_pass_fraction`` (default 0.5).
Legacy callers with the flag off see no behavior change.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from eta_engine.backtest.deflated_sharpe import compute_dsr
from eta_engine.backtest.engine import BacktestEngine

if TYPE_CHECKING:
    from eta_engine.backtest.models import BacktestConfig, Trade
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
    # Per-fold DSR gating (additive, default off for back-compat) ----------
    strict_fold_dsr_gate: bool = False
    fold_dsr_min_pass_fraction: float = Field(default=0.5, ge=0.0, le=1.0)


class WalkForwardResult(BaseModel):
    windows: list[dict] = Field(default_factory=list)
    aggregate_is_sharpe: float = 0.0
    aggregate_oos_sharpe: float = 0.0
    oos_degradation_avg: float = 0.0
    deflated_sharpe: float = 0.0
    pass_gate: bool = False
    # Per-fold DSR layer --------------------------------------------------
    per_fold_dsr: list[float] = Field(default_factory=list)
    fold_dsr_median: float = 0.0
    fold_dsr_pass_fraction: float = 0.0


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def compute_per_fold_dsr(
    sharpe: float,
    n_trades: int,
    skew: float,
    kurtosis: float,
    n_folds: int,
) -> float:
    """DSR for a single walk-forward fold.

    Equivalent to :func:`compute_dsr` with ``n_trials=n_folds``. The fold
    count is what accounts for selection bias: every fold is a separate
    trial, so the expected-max threshold rises with the number of folds.
    """
    return compute_dsr(
        sharpe=sharpe,
        n_trades=max(n_trades, 2),
        skew=skew,
        kurtosis=kurtosis,
        n_trials=max(n_folds, 1),
    )


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
        ctx_builder: object | None = None,
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
            is_cfg = base_backtest_config.model_copy(
                update={
                    "start_date": is_bars[0].timestamp,
                    "end_date": is_bars[-1].timestamp,
                }
            )
            oos_cfg = base_backtest_config.model_copy(
                update={
                    "start_date": oos_bars[0].timestamp,
                    "end_date": oos_bars[-1].timestamp,
                }
            )
            is_res = BacktestEngine(pipeline, is_cfg, ctx_builder=ctx_builder, strategy_id=f"wf-{i}-IS").run(is_bars)
            oos_res = BacktestEngine(pipeline, oos_cfg, ctx_builder=ctx_builder, strategy_id=f"wf-{i}-OOS").run(
                oos_bars
            )
            oos_skew, oos_kurt = _fold_moments_from_trades(oos_res.trades)
            win_results.append(
                {
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
                    # Per-fold distribution shape + placeholder DSR. The DSR is
                    # filled in during aggregation once we know the total fold
                    # count (selection-bias denominator).
                    "oos_skew": round(oos_skew, 6),
                    "oos_kurt": round(oos_kurt, 6),
                    "oos_dsr": 0.0,
                }
            )
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

        # Legacy aggregate DSR: mean OOS Sharpe + cross-window moments
        skew, kurt = _moments(oos_sharpes)
        dsr = compute_dsr(
            sharpe=agg_oos,
            n_trades=max(sum(w["oos_trades"] for w in wins), 2),
            skew=skew,
            kurtosis=kurt,
            n_trials=len(wins),
        )

        # Per-fold DSR layer. Fill in each window's oos_dsr using that
        # fold's trade-return moments and the total fold count.
        n_folds = len(wins)
        per_fold_dsr: list[float] = []
        for w in wins:
            fold_dsr = compute_per_fold_dsr(
                sharpe=w["oos_sharpe"],
                n_trades=w["oos_trades"],
                skew=w["oos_skew"],
                kurtosis=w["oos_kurt"],
                n_folds=n_folds,
            )
            w["oos_dsr"] = round(fold_dsr, 4)
            per_fold_dsr.append(fold_dsr)

        fold_median = _median(per_fold_dsr)
        fold_pass_frac = sum(1 for d in per_fold_dsr if d > 0.5) / n_folds if n_folds else 0.0

        # Gate: legacy checks always required; strict mode adds the
        # per-fold median + pass-fraction guardrails.
        all_met = all(w["min_trades_met"] for w in wins)
        legacy_gate = dsr > 0.5 and deg_avg < 0.35 and all_met
        if cfg.strict_fold_dsr_gate:
            gate = legacy_gate and fold_median > 0.5 and fold_pass_frac >= cfg.fold_dsr_min_pass_fraction
        else:
            gate = legacy_gate

        return WalkForwardResult(
            windows=wins,
            aggregate_is_sharpe=round(agg_is, 4),
            aggregate_oos_sharpe=round(agg_oos, 4),
            oos_degradation_avg=round(deg_avg, 4),
            deflated_sharpe=round(dsr, 4),
            pass_gate=gate,
            per_fold_dsr=[round(d, 4) for d in per_fold_dsr],
            fold_dsr_median=round(fold_median, 4),
            fold_dsr_pass_fraction=round(fold_pass_frac, 4),
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
    skew = sum((x - m) ** 3 for x in xs) / (n * sd**3)
    kurt = sum((x - m) ** 4 for x in xs) / (n * sd**4)
    return skew, kurt


def _fold_moments_from_trades(trades: list[Trade]) -> tuple[float, float]:
    """Return (skew, raw kurtosis) of the per-trade ``pnl_r`` distribution.

    Falls back to the normal-null (skew=0, kurt=3) when the fold has fewer
    than two trades or a degenerate (zero-variance) return series -- the
    DSR formula uses these as the identity case.
    """
    if not trades or len(trades) < 2:
        return 0.0, 3.0
    xs = [t.pnl_r for t in trades]
    return _moments(xs)


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return 0.5 * (s[n // 2 - 1] + s[n // 2])
