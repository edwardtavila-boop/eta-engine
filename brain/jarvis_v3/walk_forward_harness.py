"""Walk-forward harness (Wave-16, 2026-04-27).

Unified runner for any policy/strategy that takes a sequence of
samples (typically R-multiples per trade) and produces realized
outcomes. The harness:

  * Splits the sample stream into rolling train/test windows
  * Runs the policy on each train window, evaluates on test
  * Computes per-fold metrics: Sharpe, Sortino, max DD, win-rate,
    sample size, IS-vs-OOS gap
  * Aggregates: mean Sharpe, Bonferroni-corrected p-values when
    multiple gates / configs are evaluated, PSR (Bailey & Lopez de
    Prado 2014), sample-size adequacy
  * Returns WalkForwardResult with PASS / FAIL per gate

Pure stdlib + math. Reuses obs/performance_metrics where possible.

Use case (called by pre_live_gate, ab_framework, regression_test_set):

    from eta_engine.brain.jarvis_v3.walk_forward_harness import (
        run_walk_forward, WalkForwardConfig,
    )

    result = run_walk_forward(
        sample_r=journal_r_multiples,
        policy_fn=lambda window: my_policy(window),
        cfg=WalkForwardConfig(train_size=200, test_size=50, step=50),
    )
    if result.passed_gates:
        promote()
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


# ─── Config ──────────────────────────────────────────────────────


@dataclass
class WalkForwardConfig:
    """Operator-tunable knobs for the harness."""

    train_size: int = 200
    test_size: int = 50
    step: int = 50
    target_sharpe: float = 1.0
    max_dd_r: float = 6.0
    min_trades_per_fold: int = 10
    min_aggregate_trades: int = 100
    psr_threshold: float = 0.95  # 95% prob the true Sharpe > target
    bonferroni_alpha: float = 0.05  # family-wise error rate


@dataclass
class FoldResult:
    """One walk-forward fold's metrics."""

    fold_idx: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    n_trades: int
    avg_r: float
    sharpe: float
    sortino: float
    max_dd_r: float
    win_rate: float


@dataclass
class WalkForwardResult:
    """Aggregated walk-forward output with gate verdicts."""

    n_folds: int
    n_total_trades: int
    folds: list[FoldResult] = field(default_factory=list)
    aggregate_avg_r: float = 0.0
    aggregate_sharpe: float = 0.0
    aggregate_max_dd_r: float = 0.0
    aggregate_psr: float = 0.0
    is_oos_gap: float = 0.0
    sharpe_bonferroni_p: float = 0.0
    gates: dict[str, bool] = field(default_factory=dict)
    passed_gates: bool = False
    summary: str = ""

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


# ─── Stat helpers ────────────────────────────────────────────────


def _moments(rs: list[float]) -> tuple[float, float]:
    n = len(rs)
    if n < 2:
        return (rs[0] if rs else 0.0), 0.0
    m = sum(rs) / n
    var = sum((r - m) ** 2 for r in rs) / (n - 1)
    return m, math.sqrt(var)


def _sharpe(rs: list[float]) -> float:
    m, s = _moments(rs)
    return m / s if s > 0 else 0.0


def _sortino(rs: list[float]) -> float:
    m, _ = _moments(rs)
    downsides = [r for r in rs if r < 0]
    if not downsides:
        return float("inf") if m > 0 else 0.0
    sd_down = math.sqrt(sum(d * d for d in downsides) / len(downsides))
    return m / sd_down if sd_down > 0 else 0.0


def _max_drawdown(rs: list[float]) -> float:
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return max_dd


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _psr(rs: list[float], target_sharpe: float) -> float:
    """Probabilistic Sharpe Ratio (Bailey & Lopez de Prado 2014)."""
    n = len(rs)
    if n < 5:
        return 0.0
    m, s = _moments(rs)
    if s == 0:
        return 1.0 if m >= target_sharpe else 0.0
    sharpe = m / s
    skew = sum(((r - m) / s) ** 3 for r in rs) / n
    kurt = sum(((r - m) / s) ** 4 for r in rs) / n - 3.0
    se = math.sqrt(
        (1.0 - skew * sharpe + (kurt + 2) / 4 * sharpe * sharpe) / (n - 1),
    )
    if se == 0:
        return 0.0
    z = (sharpe - target_sharpe) / se
    return _norm_cdf(z)


# ─── Main runner ─────────────────────────────────────────────────


def run_walk_forward(
    *,
    sample_r: list[float],
    policy_fn: Callable[[list[float]], list[float]] | None = None,
    cfg: WalkForwardConfig | None = None,
) -> WalkForwardResult:
    """Run walk-forward over ``sample_r``.

    ``policy_fn(train_window) -> test_window`` is optional. When
    None, the harness treats sample_r as already-filtered realized R
    values and just slices it into folds for the metrics. When
    supplied, policy_fn produces the test-window outcomes from
    each train window (typical pattern: train_window calibrates
    parameters, then reapplied to test).
    """
    cfg = cfg or WalkForwardConfig()
    n = len(sample_r)
    folds: list[FoldResult] = []
    all_test_r: list[float] = []
    all_train_r: list[float] = []

    if n < cfg.train_size + cfg.test_size:
        return WalkForwardResult(
            n_folds=0,
            n_total_trades=n,
            summary=(f"insufficient data: have {n}, need at least {cfg.train_size + cfg.test_size}"),
        )

    fold_idx = 0
    start = 0
    while start + cfg.train_size + cfg.test_size <= n:
        train = sample_r[start : start + cfg.train_size]
        test_raw = sample_r[start + cfg.train_size : start + cfg.train_size + cfg.test_size]
        if policy_fn is not None:
            try:
                test = list(policy_fn(train))
                if not test:
                    test = test_raw
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "walk_forward: policy_fn raised on fold %d (%s)",
                    fold_idx,
                    exc,
                )
                test = test_raw
        else:
            test = test_raw

        if len(test) < cfg.min_trades_per_fold:
            start += cfg.step
            fold_idx += 1
            continue

        folds.append(
            FoldResult(
                fold_idx=fold_idx,
                train_start=start,
                train_end=start + cfg.train_size,
                test_start=start + cfg.train_size,
                test_end=start + cfg.train_size + cfg.test_size,
                n_trades=len(test),
                avg_r=round(sum(test) / len(test), 4),
                sharpe=round(_sharpe(test), 4),
                sortino=round(_sortino(test), 4),
                max_dd_r=round(_max_drawdown(test), 4),
                win_rate=round(sum(1 for r in test if r > 0) / len(test), 3),
            )
        )
        all_test_r.extend(test)
        all_train_r.extend(train)
        start += cfg.step
        fold_idx += 1

    if not folds:
        return WalkForwardResult(
            n_folds=0,
            n_total_trades=n,
            summary="no folds met min_trades_per_fold threshold",
        )

    # Aggregates
    agg_avg = sum(all_test_r) / len(all_test_r)
    agg_sharpe = _sharpe(all_test_r)
    agg_max_dd = _max_drawdown(all_test_r)
    agg_psr = _psr(all_test_r, cfg.target_sharpe)
    train_sharpe = _sharpe(all_train_r) if all_train_r else 0.0
    is_oos_gap = train_sharpe - agg_sharpe

    # Bonferroni p: simple two-sided z-test on aggregate Sharpe
    # against null (Sharpe == 0). p_raw = 2*(1 - Phi(z)) where
    # z = sharpe * sqrt(n)
    n_test = len(all_test_r)
    z_stat = agg_sharpe * math.sqrt(max(n_test, 1))
    p_raw = 2.0 * (1.0 - _norm_cdf(abs(z_stat)))
    n_tests_in_family = max(1, len(folds))
    sharpe_bonf_p = min(1.0, p_raw * n_tests_in_family)

    # Gates
    gates = {
        "min_trades": n_test >= cfg.min_aggregate_trades,
        "sharpe_above_target": agg_sharpe >= cfg.target_sharpe,
        "max_dd_within_budget": agg_max_dd <= cfg.max_dd_r,
        "psr_above_threshold": agg_psr >= cfg.psr_threshold,
        "bonferroni_significant": sharpe_bonf_p <= cfg.bonferroni_alpha,
    }
    passed = all(gates.values())

    summary = (
        f"walk-forward: {len(folds)} folds, {n_test} test trades, "
        f"agg sharpe={agg_sharpe:.2f}, PSR={agg_psr:.2f}, "
        f"max DD={agg_max_dd:.2f}R, "
        f"IS-OOS gap={is_oos_gap:+.2f}, "
        f"bonf-p={sharpe_bonf_p:.3f} -> "
        f"{'PASS' if passed else 'FAIL'}"
    )

    return WalkForwardResult(
        n_folds=len(folds),
        n_total_trades=n_test,
        folds=folds,
        aggregate_avg_r=round(agg_avg, 4),
        aggregate_sharpe=round(agg_sharpe, 4),
        aggregate_max_dd_r=round(agg_max_dd, 4),
        aggregate_psr=round(agg_psr, 4),
        is_oos_gap=round(is_oos_gap, 4),
        sharpe_bonferroni_p=round(sharpe_bonf_p, 6),
        gates=gates,
        passed_gates=passed,
        summary=summary,
    )
