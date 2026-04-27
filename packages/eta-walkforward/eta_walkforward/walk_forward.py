"""
eta_walkforward.walk_forward
============================
The strict walk-forward gate.

Engine-agnostic. You compute walk-forward windows in your own engine
(this package doesn't presume a backtest implementation) and pass the
per-window stats here as ``WindowStats`` objects. The gate evaluates
the aggregate against three configurable layers — strict per-fold
DSR, long-haul positive-fold-fraction, or grid-mode profit-factor —
and returns a single ``WalkForwardResult`` with ``pass_gate``.

If you want a turnkey runner, the README has a copy-paste reference
implementation using the standard "split bars into IS/OOS windows,
backtest each, aggregate" pattern.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from eta_walkforward.deflated_sharpe import compute_dsr


# ---------------------------------------------------------------------------
# Config / per-window input / aggregate result
# ---------------------------------------------------------------------------


class WalkForwardConfig(BaseModel):
    """All gate knobs in one place. Sensible defaults — override only
    when the strategy you're evaluating has a different cadence."""

    window_days: int = Field(default=60, ge=1)
    step_days: int = Field(default=30, ge=1)
    anchored: bool = False
    oos_fraction: float = Field(default=0.3, gt=0.0, lt=1.0)
    min_trades_per_window: int = Field(default=20, ge=1)
    # Fraction of windows that must individually meet the
    # ``min_trades_per_window`` threshold for the legacy gate to pass.
    # 1.0 = strict (every window), 0.8 = "most" (default — accommodates
    # selective strategies that fire only a handful of trades per OOS
    # window).
    min_trades_met_fraction: float = Field(default=0.8, ge=0.0, le=1.0)
    # Per-fold DSR gating (additive, default off for back-compat).
    strict_fold_dsr_gate: bool = False
    fold_dsr_min_pass_fraction: float = Field(default=0.5, ge=0.0, le=1.0)
    # Long-haul mode: for daily/weekly bots that fire 1-3 trades per
    # OOS fold, the per-fold DSR gate is the wrong shape — DSR
    # estimation needs ~10+ trades to stabilize. When True, the gate
    # uses positive-fold-fraction instead of per-fold DSR pass.
    long_haul_mode: bool = False
    long_haul_min_pos_fraction: float = Field(default=0.55, ge=0.0, le=1.0)
    # Grid-mode gate: for market-making strategies where Sharpe is the
    # wrong metric. Profit-factor + bounded drawdown + pos-fraction.
    grid_mode: bool = False
    grid_min_profit_factor: float = Field(default=1.3, gt=0.0)
    grid_max_dd_pct: float = Field(default=20.0, gt=0.0)
    grid_min_pos_fraction: float = Field(default=0.55, ge=0.0, le=1.0)


class WindowStats(BaseModel):
    """Per-window stats your engine produces.

    The walk-forward gate consumes a list of these and computes the
    aggregate verdict. Every field is required so the gate doesn't
    silently treat missing data as "passing".
    """

    window_index: int = Field(ge=0)
    is_sharpe: float
    oos_sharpe: float
    is_trades: int = Field(ge=0)
    oos_trades: int = Field(ge=0)
    # Distribution moments of the OOS per-trade R series. Used by DSR.
    # If the OOS sample has < 2 trades, pass (skew=0, kurt=3) — the
    # normal-null fallback.
    oos_skew: float = 0.0
    oos_kurt: float = 3.0
    # Optional fields used only by grid_mode. Provide them when you
    # plan to evaluate against grid-mode; otherwise leave as defaults.
    oos_profit_factor: float = 0.0
    oos_max_dd_pct: float = 0.0


class WalkForwardResult(BaseModel):
    """Aggregate output. ``pass_gate`` is the final True/False answer."""

    windows: list[WindowStats] = Field(default_factory=list)
    aggregate_is_sharpe: float = 0.0
    aggregate_oos_sharpe: float = 0.0
    aggregate_oos_degradation: float = 0.0
    oos_degradation_avg: float = 0.0
    deflated_sharpe: float = 0.0
    pass_gate: bool = False
    per_fold_dsr: list[float] = Field(default_factory=list)
    fold_dsr_median: float = 0.0
    fold_dsr_pass_fraction: float = 0.0
    # Reasons the gate said pass / fail — printable.
    reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def _degradation(is_sharpe: float, oos_sharpe: float) -> float:
    """Per-window IS->OOS degradation, clamped to [0, 1].

    The clamp is load-bearing: when IS is small (say +0.01) and OOS
    drops to -1.0, the raw ratio explodes to 100x. Averaging that
    across folds poisons the gate. Clamping to 1.0 captures the
    "OOS went the wrong direction" outcome without overweighting noise.
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


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def evaluate_gate(
    cfg: WalkForwardConfig,
    windows: list[WindowStats],
) -> WalkForwardResult:
    """Compute the walk-forward verdict from per-window stats.

    Use this when you've already run a walk-forward in your own engine
    and want the gate's opinion. The returned ``pass_gate`` is the
    final True/False answer.

    Returns an empty result with ``pass_gate=False`` when there are
    no windows.
    """
    if not windows:
        return WalkForwardResult(
            reasons=["no walk-forward windows provided"],
        )

    n_folds = len(windows)
    is_sharpes = [w.is_sharpe for w in windows]
    oos_sharpes = [w.oos_sharpe for w in windows]
    agg_is = sum(is_sharpes) / n_folds
    agg_oos = sum(oos_sharpes) / n_folds

    degradations = [_degradation(w.is_sharpe, w.oos_sharpe) for w in windows]
    deg_avg = sum(degradations) / n_folds

    # Aggregate-level degradation. For long-haul strategies on small
    # IS Sharpes, the per-window-avg deg is dominated by noise; the
    # aggregate measure asks the right question — "did the strategy as
    # a whole degrade?".
    agg_deg = max(
        (agg_is - agg_oos) / agg_is if agg_is > 0.0 else 0.0,
        0.0,
    )

    # Cross-window DSR moments
    skew, kurt = _moments(oos_sharpes)
    n_trades_total = max(sum(w.oos_trades for w in windows), 2)
    dsr = compute_dsr(
        sharpe=agg_oos,
        n_trades=n_trades_total,
        skew=skew,
        kurtosis=kurt,
        n_trials=n_folds,
    )

    # Per-fold DSR (used by strict_fold_dsr_gate)
    per_fold_dsr: list[float] = []
    for w in windows:
        fold_dsr = compute_dsr(
            sharpe=w.oos_sharpe,
            n_trades=max(w.oos_trades, 2),
            skew=w.oos_skew,
            kurtosis=w.oos_kurt,
            n_trials=n_folds,
        )
        per_fold_dsr.append(fold_dsr)
    fold_median = _median(per_fold_dsr)
    fold_pass_frac = (
        sum(1 for d in per_fold_dsr if d > 0.5) / n_folds
        if n_folds else 0.0
    )

    # min_trades coverage
    n_met = sum(
        1 for w in windows
        if w.is_trades >= cfg.min_trades_per_window
        and w.oos_trades >= cfg.min_trades_per_window
    )
    met_frac = n_met / n_folds
    all_met = met_frac >= cfg.min_trades_met_fraction

    is_positive = agg_is > 0.0

    reasons: list[str] = []
    legacy_gate = (
        dsr > 0.5 and deg_avg < 0.35 and all_met and is_positive
    )
    if not is_positive:
        reasons.append(f"agg IS Sharpe {agg_is:+.3f} not positive")
    if dsr <= 0.5:
        reasons.append(f"aggregate DSR {dsr:.3f} <= 0.5")
    if deg_avg >= 0.35:
        reasons.append(f"per-window deg avg {deg_avg:.3f} >= 0.35")
    if not all_met:
        reasons.append(
            f"min_trades met {met_frac * 100:.0f}% < "
            f"{cfg.min_trades_met_fraction * 100:.0f}%",
        )

    if cfg.grid_mode:
        n_pos = sum(1 for w in windows if w.oos_sharpe > 0.0)
        pos_frac = n_pos / n_folds
        pfs = sorted(
            min(float(w.oos_profit_factor or 0.0), 10.0) for w in windows
        )
        agg_pf = pfs[len(pfs) // 2] if pfs else 0.0
        worst_dd = max(
            (float(w.oos_max_dd_pct or 0.0) for w in windows),
            default=0.0,
        )
        grid_total_return_positive = agg_oos > 0.0 or pos_frac > 0.5
        gate = (
            grid_total_return_positive
            and agg_pf >= cfg.grid_min_profit_factor
            and worst_dd <= cfg.grid_max_dd_pct
            and pos_frac >= cfg.grid_min_pos_fraction
        )
        if not gate:
            if agg_pf < cfg.grid_min_profit_factor:
                reasons.append(
                    f"profit factor {agg_pf:.2f} < {cfg.grid_min_profit_factor:.2f}",
                )
            if worst_dd > cfg.grid_max_dd_pct:
                reasons.append(
                    f"worst-fold DD {worst_dd:.1f}% > {cfg.grid_max_dd_pct:.1f}%",
                )
            if pos_frac < cfg.grid_min_pos_fraction:
                reasons.append(
                    f"positive-fold {pos_frac * 100:.0f}% < "
                    f"{cfg.grid_min_pos_fraction * 100:.0f}%",
                )
    elif cfg.long_haul_mode:
        n_pos = sum(1 for w in windows if w.oos_sharpe > 0.0)
        pos_frac = n_pos / n_folds
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
        if pos_frac < cfg.long_haul_min_pos_fraction:
            reasons.append(
                f"positive-fold {pos_frac * 100:.0f}% < "
                f"{cfg.long_haul_min_pos_fraction * 100:.0f}%",
            )
        if agg_deg >= 0.35:
            reasons.append(f"aggregate deg {agg_deg:.3f} >= 0.35")
    elif cfg.strict_fold_dsr_gate:
        gate = (
            legacy_gate
            and fold_median > 0.5
            and fold_pass_frac >= cfg.fold_dsr_min_pass_fraction
        )
        if fold_median <= 0.5:
            reasons.append(f"fold DSR median {fold_median:.3f} <= 0.5")
        if fold_pass_frac < cfg.fold_dsr_min_pass_fraction:
            reasons.append(
                f"fold DSR pass-fraction {fold_pass_frac * 100:.0f}% < "
                f"{cfg.fold_dsr_min_pass_fraction * 100:.0f}%",
            )
    else:
        gate = legacy_gate

    if gate:
        reasons = [f"PASS — agg IS {agg_is:+.3f} / OOS {agg_oos:+.3f} / DSR {dsr:.3f}"]

    return WalkForwardResult(
        windows=windows,
        aggregate_is_sharpe=round(agg_is, 4),
        aggregate_oos_sharpe=round(agg_oos, 4),
        aggregate_oos_degradation=round(agg_deg, 4),
        oos_degradation_avg=round(deg_avg, 4),
        deflated_sharpe=round(dsr, 4),
        pass_gate=gate,
        per_fold_dsr=[round(d, 4) for d in per_fold_dsr],
        fold_dsr_median=round(fold_median, 4),
        fold_dsr_pass_fraction=round(fold_pass_frac, 4),
        reasons=reasons,
    )
