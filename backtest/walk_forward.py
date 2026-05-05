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
distribution moments and ``sweep_n * n_folds`` as the trial count. We expose:

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
    # Fraction of windows that must individually meet the
    # ``min_trades_per_window`` threshold for the legacy gate to pass.
    # 1.0 = strict (every window), 0.8 = "most" (default — accommodates
    # selective strategies that fire only a handful of trades per OOS
    # window). The DSR pass-fraction gate already guards against
    # pathological few-trade windows at the fold level, so requiring
    # 100pct here was duplicate strictness.
    min_trades_met_fraction: float = Field(default=0.8, ge=0.0, le=1.0)
    # Multiple-testing penalty: every parameter-sweep cell times every
    # walk-forward fold is one selection trial for DSR.
    sweep_n: int = Field(default=1, ge=1)
    # Per-fold DSR gating (additive, default off for back-compat) ----------
    strict_fold_dsr_gate: bool = False
    fold_dsr_min_pass_fraction: float = Field(default=0.5, ge=0.0, le=1.0)
    # Long-haul mode: for daily/weekly bots that fire 1-3 trades per
    # OOS fold, the per-fold DSR gate is the wrong shape — DSR
    # estimation needs ~10+ trades to stabilize. When True, the
    # strict fold gate uses pos-fraction (fraction of folds with
    # OOS Sharpe > 0) instead of DSR pass-fraction, AND the gate
    # also requires the FOLD-DISTRIBUTION-LEVEL DSR (the legacy
    # aggregate ``deflated_sharpe``) to clear 0.5. This is the
    # principled "I have edge across many folds but each fold is
    # too small to compute a reliable DSR" path. 2026-04-27.
    long_haul_mode: bool = False
    long_haul_min_pos_fraction: float = Field(default=0.55, ge=0.0, le=1.0)
    # Grid-mode gate: for market-making / liquidity-providing
    # strategies (grid trading) where Sharpe is the wrong metric.
    # Grid trading produces many small approximately-equal-magnitude
    # wins and losses; the right measure is profit factor (sum of
    # winning PnL / sum of losing PnL) and bounded drawdown. When
    # ``grid_mode`` is True the gate becomes:
    #   profit_factor > grid_min_profit_factor (default 1.3)
    #   AND total_return > 0 AND max_dd < grid_max_dd_pct
    #   AND positive-fold-fraction >= grid_min_pos_fraction
    # The standard DSR / degradation / IS-positive checks are
    # bypassed because they punish low-Sharpe-but-positive-edge
    # strategies that are exactly what grid trading is.
    grid_mode: bool = False
    grid_min_profit_factor: float = Field(default=1.3, gt=0.0)
    grid_max_dd_pct: float = Field(default=20.0, gt=0.0)
    grid_min_pos_fraction: float = Field(default=0.55, ge=0.0, le=1.0)
    # Aggregate-degradation mode: use agg_deg (aggregate IS->OOS gap)
    # instead of per-window-avg deg_avg for the degradation check in
    # the standard gate path. Crypto 1h strategies with 21+ windows
    # often have a single regime-shift outlier window that blows up
    # the per-window average while the aggregate strategy actually
    # IMPROVES OOS over IS. This mode is the principled fix: it
    # keeps the per-fold DSR gate but swaps the degradation measure
    # for the one the long-haul gate already uses. 2026-04-30.
    agg_degradation_mode: bool = False


class WalkForwardResult(BaseModel):
    windows: list[dict] = Field(default_factory=list)
    aggregate_is_sharpe: float = 0.0
    aggregate_oos_sharpe: float = 0.0
    oos_degradation_avg: float = 0.0
    deflated_sharpe: float = 0.0
    dsr_n_trials: int = 0
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
    sweep_n: int = 1,
) -> float:
    """DSR for a single walk-forward fold.

    Equivalent to :func:`compute_dsr` with ``n_trials=sweep_n * n_folds``.
    Every tested parameter cell across every fold is a separate trial, so the
    expected-max threshold rises with both sweep breadth and WF window count.
    """
    n_trials = max(n_folds, 1) * max(sweep_n, 1)
    return compute_dsr(
        sharpe=sharpe,
        n_trades=max(n_trades, 2),
        skew=skew,
        kurtosis=kurtosis,
        n_trials=n_trials,
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
        scorer: object | None = None,
        block_regimes: frozenset[str] | set[str] | None = None,
        require_ctx_true: tuple[str, ...] | None = None,
        strategy_factory: object | None = None,
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
            # strategy_factory: a zero-arg callable that returns a fresh
            # strategy instance for each window. Required because
            # strategies hold per-day state — sharing across IS/OOS
            # windows would leak state. None = use confluence path.
            is_strategy = strategy_factory() if strategy_factory else None
            oos_strategy = strategy_factory() if strategy_factory else None
            # If the strategy exposes ``on_trade_close``, wire it as
            # the engine's trade-close callback. Duck-typed so any
            # strategy can opt in (AdaptiveKellySizing is the
            # canonical consumer; others may follow). The callback
            # fires once per realized trade with the full Trade obj
            # (pnl_r, pnl_usd, side, etc.) — proper signal vs the
            # legacy equity-delta inference path.
            is_cb = getattr(is_strategy, "on_trade_close", None) if is_strategy else None
            oos_cb = getattr(oos_strategy, "on_trade_close", None) if oos_strategy else None
            is_res = BacktestEngine(
                pipeline, is_cfg, ctx_builder=ctx_builder, strategy_id=f"wf-{i}-IS",
                scorer=scorer, block_regimes=block_regimes,
                require_ctx_true=require_ctx_true, strategy=is_strategy,
                on_trade_close=is_cb,
            ).run(is_bars)
            oos_res = BacktestEngine(
                pipeline, oos_cfg, ctx_builder=ctx_builder, strategy_id=f"wf-{i}-OOS",
                scorer=scorer, block_regimes=block_regimes,
                require_ctx_true=require_ctx_true, strategy=oos_strategy,
                on_trade_close=oos_cb,
            ).run(oos_bars)
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
                    "oos_profit_factor": oos_res.profit_factor,
                    "oos_max_dd_pct": oos_res.max_dd_pct,
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
        n_folds = len(wins)
        dsr_n_trials = max(n_folds * cfg.sweep_n, 1)
        dsr = compute_dsr(
            sharpe=agg_oos,
            n_trades=max(sum(w["oos_trades"] for w in wins), 2),
            skew=skew,
            kurtosis=kurt,
            n_trials=dsr_n_trials,
        )

        # Per-fold DSR layer. Fill in each window's oos_dsr using that
        # fold's trade-return moments and the full sweep-adjusted trial count.
        per_fold_dsr: list[float] = []
        for w in wins:
            fold_dsr = compute_per_fold_dsr(
                sharpe=w["oos_sharpe"],
                n_trades=w["oos_trades"],
                skew=w["oos_skew"],
                kurtosis=w["oos_kurt"],
                n_folds=n_folds,
                sweep_n=cfg.sweep_n,
            )
            w["oos_dsr"] = round(fold_dsr, 4)
            per_fold_dsr.append(fold_dsr)

        fold_median = _median(per_fold_dsr)
        fold_pass_frac = sum(1 for d in per_fold_dsr if d > 0.5) / n_folds if n_folds else 0.0

        # Gate: legacy checks always required; strict mode adds the
        # per-fold median + pass-fraction guardrails.
        # `min_trades_met_fraction` defaults to 0.8 (was strict `all`
        # before 2026-04-27) — selective crypto strategies fire 2-8
        # trades per OOS window, so demanding every single window
        # clear the threshold is duplicate strictness vs the per-fold
        # DSR pass-fraction gate. Set to 1.0 for the original behaviour.
        n_met = sum(1 for w in wins if w["min_trades_met"])
        met_frac = n_met / n_folds if n_folds else 0.0
        all_met = met_frac >= cfg.min_trades_met_fraction
        # Require a positive aggregate IN-SAMPLE Sharpe. Without this
        # check, a strategy with persistently negative IS but lucky-
        # date-split positive OOS would PASS — the engine's
        # `_degradation` returns 0 when IS<=0, masking the bad IS.
        # (Surfaced 2026-04-27 by ETH crypto_orb tuned config: agg
        # IS -3.02, agg OOS +3.57. Walk-forward should validate
        # IS+ AND OOS+, not just OOS+.)
        is_positive = agg_is > 0.0
        # Aggregate-level degradation: IS->OOS gap measured against
        # the aggregate IS Sharpe, not averaged across noisy per-
        # window ratios. For long-haul / high-variance bots the
        # per-window deg avg is dominated by small-IS folds; the
        # aggregate measure asks the right question — "did the
        # strategy as a whole degrade?". Negative means improvement;
        # we clamp at 0 so the gate sees "no degradation."
        agg_deg = max(
            (agg_is - agg_oos) / agg_is if agg_is > 0.0 else 0.0,
            0.0,
        )
        deg_check = agg_deg if cfg.agg_degradation_mode else deg_avg
        legacy_gate = dsr > 0.5 and deg_check < 0.35 and all_met and is_positive
        if cfg.grid_mode:
            # Grid-mode gate: profit_factor + bounded DD + positive
            # consistency. Sharpe / DSR are bypassed because they
            # punish low-Sharpe-but-positive-edge market-making
            # profiles. Aggregate profit factor across all folds
            # weighted by their PnL volume.
            n_pos_folds = sum(1 for w in wins if w.get("oos_sharpe", 0.0) > 0.0)
            pos_frac = n_pos_folds / n_folds if n_folds else 0.0
            # Aggregate profit factor: median of fold PFs (robust to
            # one fold dominating). Folds with no losing trades have
            # PF set to a large sentinel (BacktestResult convention),
            # so we cap to 10.0 for the median.
            pfs = [
                min(float(w.get("oos_profit_factor", 0.0) or 0.0), 10.0)
                for w in wins
            ]
            pfs.sort()
            agg_pf = pfs[len(pfs) // 2] if pfs else 0.0
            # Worst-fold drawdown across all OOS windows.
            worst_dd = max(
                (float(w.get("oos_max_dd_pct", 0.0) or 0.0) for w in wins),
                default=0.0,
            )
            grid_total_return_positive = agg_oos > 0.0 or pos_frac > 0.5
            gate = (
                grid_total_return_positive
                and agg_pf >= cfg.grid_min_profit_factor
                and worst_dd <= cfg.grid_max_dd_pct
                and pos_frac >= cfg.grid_min_pos_fraction
            )
        elif cfg.long_haul_mode:
            # Long-haul gate: skip per-fold DSR (unreliable on 1-3
            # trade folds), use aggregate-level degradation instead
            # of per-window-avg, and require fraction-of-folds with
            # positive OOS Sharpe to clear ``long_haul_min_pos_
            # fraction``. The aggregate DSR (>0.5) + agg_deg<0.35
            # + pos-fraction is the principled path for daily/weekly
            # cadence bots that fire too few trades per fold for
            # the per-fold DSR + per-window-avg-deg shape to work.
            n_pos_folds = sum(1 for w in wins if w.get("oos_sharpe", 0.0) > 0.0)
            pos_frac = n_pos_folds / n_folds if n_folds else 0.0
            long_haul_legacy = (
                dsr > 0.5
                and agg_deg < 0.35
                and all_met
                and is_positive
            )
            gate = (
                long_haul_legacy
                and pos_frac >= cfg.long_haul_min_pos_fraction
            )
        elif cfg.strict_fold_dsr_gate:
            gate = legacy_gate and fold_median > 0.5 and fold_pass_frac >= cfg.fold_dsr_min_pass_fraction
        else:
            gate = legacy_gate

        return WalkForwardResult(
            windows=wins,
            aggregate_is_sharpe=round(agg_is, 4),
            aggregate_oos_sharpe=round(agg_oos, 4),
            oos_degradation_avg=round(deg_avg, 4),
            deflated_sharpe=round(dsr, 4),
            dsr_n_trials=dsr_n_trials,
            pass_gate=gate,
            per_fold_dsr=[round(d, 4) for d in per_fold_dsr],
            fold_dsr_median=round(fold_median, 4),
            fold_dsr_pass_fraction=round(fold_pass_frac, 4),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _degradation(is_sharpe: float, oos_sharpe: float) -> float:
    """Per-window IS->OOS degradation, clamped to [0, 1].

    The clamp is load-bearing: when IS is small (say +0.01) and OOS
    drops to -1.0, the raw ratio explodes to 100x. Averaging that
    across folds poisons ``oos_degradation_avg`` and the gate's
    ``deg_avg < 0.35`` check fails for honest strategies that
    produced strong aggregate OOS but had a few small-IS folds.

    Semantically, "deg > 1.0" means OOS went the WRONG direction
    (negative when IS was positive). That outcome is the SAME
    regardless of magnitude — the strategy degraded fully.
    Clamping to 1.0 captures this without overweighting noise.
    """
    if is_sharpe <= 0.0:
        return 0.0 if oos_sharpe >= is_sharpe else 1.0
    raw = (is_sharpe - oos_sharpe) / is_sharpe
    return round(max(min(raw, 1.0), 0.0), 4)


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
