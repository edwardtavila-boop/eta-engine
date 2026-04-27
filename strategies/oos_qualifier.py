"""EVOLUTIONARY TRADING ALGO  //  strategies.oos_qualifier.

Per-strategy walk-forward + DSR qualification on top of
:mod:`strategies.backtest_harness`.

Why this module exists
----------------------
The harness already runs the six AI-Optimized strategies over a bar
tape and spits out per-strategy R-multiple statistics (hit rate,
avg R, trade count). What it does NOT do is decide whether a given
strategy is *real* -- whether its backtest edge is likely to survive
out-of-sample deployment, or whether it is an artefact of fitting on
this particular tape.

:func:`qualify_strategies` sits on top of ``run_harness`` and answers
the *keep-or-kill* question per strategy per asset:

1. Split the bar tape into ``n_windows`` rolling IS/OOS pairs.
2. Run the harness twice per window -- once on the IS bars, once on
   the OOS bars.
3. For each strategy, compute a per-trade Sharpe-like statistic from
   the R-multiple distribution on the IS tape and on the OOS tape.
4. Aggregate across windows: average IS/OOS Sharpe, degradation, DSR
   using the cross-window OOS Sharpes as the null distribution.
5. Apply the :class:`QualificationGate` (DSR, degradation, min trades).

Output: a :class:`QualificationReport` listing every strategy that was
exercised on the tape and whether it passed the gate. Live policy
callers (``eta_engine.bots.*``) can intersect the report against
the DEFAULT_ELIGIBILITY table to build a runtime allowlist -- a
strategy that fails the gate on an asset is excluded from that
asset's router dispatch until a future run re-qualifies it.

Design choices
--------------
* **Sharpe-like from R-multiples** -- per-trade mean / per-trade
  stddev. Not annualised. Matches what DSR expects (a scale-invariant
  per-sample ratio with the per-sample moments). Zero stddev (all
  trades flat or identical) -> 0 Sharpe.
* **Kurtosis input is raw kurtosis** (3.0 for a normal), to match
  :func:`compute_dsr`'s contract.
* **Non-anchored rolling windows** -- each window is a contiguous
  slice; the next window starts after the previous window's OOS end.
  This avoids data reuse across windows.
* **n_trials for DSR** = max(n_windows, 1). Every window is one
  independent trial of the strategy's edge, so the Gumbel correction
  deflates more aggressively when more windows are tested.
* **Failure reasons are explicit** -- each qualification carries a
  tuple of strings describing why it failed, so downstream dashboards
  can display the drop cause without re-running the analysis.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from eta_engine.backtest.deflated_sharpe import compute_dsr
from eta_engine.strategies.backtest_harness import HarnessConfig, run_harness

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.strategies.eta_policy import StrategyContext
    from eta_engine.strategies.backtest_harness import (
        BacktestReport,
        StrategyTrade,
    )
    from eta_engine.strategies.models import (
        Bar,
        StrategyId,
        StrategySignal,
    )


__all__ = [
    "DEFAULT_QUALIFICATION_GATE",
    "PerStrategyWindow",
    "QualificationGate",
    "QualificationReport",
    "StrategyQualification",
    "qualify_strategies",
]


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualificationGate:
    """Thresholds every strategy must clear to earn a PASS verdict.

    * ``dsr_threshold`` -- Deflated Sharpe Ratio required on the
      cross-window OOS distribution. Default 0.5 (Lopez de Prado's
      commonly-cited floor for "probably real"). Scale: probability
      that true SR exceeds the expected max under the null.
    * ``max_degradation_pct`` -- OOS Sharpe is allowed to be at most
      ``max_degradation_pct`` worse than IS Sharpe, averaged across
      windows. Default 0.35 -- if a strategy loses more than 35% of
      its edge in OOS, that's a curve-fit red flag.
    * ``min_trades_per_window`` -- every window must produce at least
      this many trades on both the IS and OOS tapes. A strategy that
      fires once per quarter has no claim to a real edge no matter
      how high its Sharpe looks.
    """

    dsr_threshold: float = 0.5
    max_degradation_pct: float = 0.35
    min_trades_per_window: int = 20


DEFAULT_QUALIFICATION_GATE: QualificationGate = QualificationGate()


# ---------------------------------------------------------------------------
# Per-window / per-strategy result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerStrategyWindow:
    """One strategy's IS/OOS performance on one walk-forward window."""

    strategy: StrategyId
    window_id: int
    is_n_trades: int
    is_sharpe_like: float
    is_total_r: float
    is_hit_rate: float
    oos_n_trades: int
    oos_sharpe_like: float
    oos_total_r: float
    oos_hit_rate: float
    degradation_pct: float
    min_trades_met: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy.value,
            "window_id": self.window_id,
            "is_n_trades": self.is_n_trades,
            "is_sharpe_like": round(self.is_sharpe_like, 4),
            "is_total_r": round(self.is_total_r, 4),
            "is_hit_rate": round(self.is_hit_rate, 4),
            "oos_n_trades": self.oos_n_trades,
            "oos_sharpe_like": round(self.oos_sharpe_like, 4),
            "oos_total_r": round(self.oos_total_r, 4),
            "oos_hit_rate": round(self.oos_hit_rate, 4),
            "degradation_pct": round(self.degradation_pct, 4),
            "min_trades_met": self.min_trades_met,
        }


@dataclass(frozen=True, slots=True)
class StrategyQualification:
    """Cross-window verdict for a single strategy on a single asset."""

    strategy: StrategyId
    asset: str
    n_windows: int
    avg_is_sharpe: float
    avg_oos_sharpe: float
    avg_degradation_pct: float
    dsr: float
    n_trades_is_total: int
    n_trades_oos_total: int
    passes_gate: bool
    fail_reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy.value,
            "asset": self.asset,
            "n_windows": self.n_windows,
            "avg_is_sharpe": round(self.avg_is_sharpe, 4),
            "avg_oos_sharpe": round(self.avg_oos_sharpe, 4),
            "avg_degradation_pct": round(self.avg_degradation_pct, 4),
            "dsr": round(self.dsr, 4),
            "n_trades_is_total": self.n_trades_is_total,
            "n_trades_oos_total": self.n_trades_oos_total,
            "passes_gate": self.passes_gate,
            "fail_reasons": list(self.fail_reasons),
        }


@dataclass(frozen=True, slots=True)
class QualificationReport:
    """Top-level report returned by :func:`qualify_strategies`."""

    asset: str
    gate: QualificationGate
    n_windows_requested: int
    n_windows_executed: int
    per_window: tuple[PerStrategyWindow, ...]
    qualifications: tuple[StrategyQualification, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passing_strategies(self) -> tuple[StrategyId, ...]:
        return tuple(q.strategy for q in self.qualifications if q.passes_gate)

    @property
    def failing_strategies(self) -> tuple[StrategyId, ...]:
        return tuple(q.strategy for q in self.qualifications if not q.passes_gate)

    def as_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "gate": {
                "dsr_threshold": self.gate.dsr_threshold,
                "max_degradation_pct": self.gate.max_degradation_pct,
                "min_trades_per_window": self.gate.min_trades_per_window,
            },
            "n_windows_requested": self.n_windows_requested,
            "n_windows_executed": self.n_windows_executed,
            "per_window": [w.as_dict() for w in self.per_window],
            "qualifications": [q.as_dict() for q in self.qualifications],
            "notes": list(self.notes),
            "passing_strategies": [s.value for s in self.passing_strategies],
            "failing_strategies": [s.value for s in self.failing_strategies],
        }


# ---------------------------------------------------------------------------
# Moments / Sharpe-like
# ---------------------------------------------------------------------------


def _sharpe_like(rs: list[float]) -> float:
    """Per-trade Sharpe-like: mean / stddev over R-multiples."""
    n = len(rs)
    if n < 2:
        return 0.0
    m = sum(rs) / n
    var = sum((r - m) ** 2 for r in rs) / (n - 1)
    if var <= 0.0:
        return 0.0
    return m / math.sqrt(var)


def _moments(rs: list[float]) -> tuple[float, float]:
    """Return (skew, raw_kurtosis) of an R-multiple distribution.

    Raw kurtosis = 3.0 for a normal distribution, matching
    :func:`compute_dsr`'s input contract (not excess kurtosis).
    """
    n = len(rs)
    if n < 2:
        return 0.0, 3.0
    m = sum(rs) / n
    var = sum((r - m) ** 2 for r in rs) / n
    if var <= 0.0:
        return 0.0, 3.0
    sd = math.sqrt(var)
    skew = sum((r - m) ** 3 for r in rs) / (n * sd**3)
    kurt = sum((r - m) ** 4 for r in rs) / (n * sd**4)
    return skew, kurt


def _degradation(is_sharpe: float, oos_sharpe: float) -> float:
    """IS->OOS drop as a fraction of IS Sharpe. Clamped to [0, inf)."""
    if is_sharpe <= 0.0:
        # If IS Sharpe is non-positive there is no credible edge to
        # begin with; any OOS Sharpe above IS counts as zero
        # degradation, anything worse counts as a full-degradation hit.
        return 0.0 if oos_sharpe >= is_sharpe else 1.0
    return max((is_sharpe - oos_sharpe) / is_sharpe, 0.0)


# ---------------------------------------------------------------------------
# Window builder
# ---------------------------------------------------------------------------


def _build_windows(
    total_bars: int,
    *,
    warmup_bars: int,
    n_windows: int,
    is_fraction: float,
) -> list[tuple[int, int, int]]:
    """Produce ``(is_start, is_end, oos_end)`` index triples.

    Splits the usable span (``warmup_bars..total_bars``) into
    ``n_windows`` contiguous slices. Each slice's first
    ``is_fraction`` portion is IS; the remainder is OOS. Returns the
    list of index triples that actually fit in the tape.

    Per-window index semantics: slices are half-open on the right
    (``is_start..is_end`` means ``bars[is_start:is_end]``). The
    harness's own warmup (``HarnessConfig.warmup_bars``) will apply
    on top of each slice.
    """
    if n_windows <= 0 or total_bars <= warmup_bars:
        return []
    usable = total_bars - warmup_bars
    win_size = usable // n_windows
    if win_size <= 1:
        return []
    triples: list[tuple[int, int, int]] = []
    for w in range(n_windows):
        is_start = warmup_bars + w * win_size
        win_end = is_start + win_size
        if win_end > total_bars:
            break
        is_len = max(int(round(win_size * is_fraction)), 1)
        is_end = is_start + is_len
        oos_end = win_end
        if is_end >= oos_end:
            break
        triples.append((is_start, is_end, oos_end))
    return triples


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def qualify_strategies(
    bars: list[Bar],
    asset: str,
    *,
    gate: QualificationGate = DEFAULT_QUALIFICATION_GATE,
    n_windows: int = 4,
    is_fraction: float = 0.7,
    ctx_builder: Callable[[Bar], StrategyContext] | None = None,
    harness_config: HarnessConfig | None = None,
    eligibility: dict[str, tuple[StrategyId, ...]] | None = None,
    registry: dict[StrategyId, Callable[..., StrategySignal]] | None = None,
) -> QualificationReport:
    """Walk-forward qualify every strategy the harness exercises on ``bars``.

    Parameters
    ----------
    bars:
        Oldest-first list of :class:`Bar`. Must be monotonic in ``ts``.
        At least ``warmup_bars + n_windows * 2`` bars are required to
        produce any windows.
    asset:
        Symbol ticker. Upper-cased for the report.
    gate:
        Pass thresholds. Defaults to
        :data:`DEFAULT_QUALIFICATION_GATE` (DSR>0.5, deg<35%, 20+ trades).
    n_windows, is_fraction:
        Walk-forward shape. 4 windows of 70/30 IS/OOS is the default.
    ctx_builder, harness_config, eligibility, registry:
        Forwarded to :func:`run_harness` on each IS and OOS slice.

    Returns
    -------
    :class:`QualificationReport` with per-window + per-strategy verdicts.
    """
    cfg = harness_config or HarnessConfig()
    triples = _build_windows(
        total_bars=len(bars),
        warmup_bars=cfg.warmup_bars,
        n_windows=n_windows,
        is_fraction=is_fraction,
    )
    notes: list[str] = []
    if not triples:
        notes.append("insufficient_bars_no_windows")
        return QualificationReport(
            asset=asset.upper(),
            gate=gate,
            n_windows_requested=n_windows,
            n_windows_executed=0,
            per_window=(),
            qualifications=(),
            notes=tuple(notes),
        )

    per_window: list[PerStrategyWindow] = []
    # trade-R lists pooled per-strategy across windows, used for DSR
    pooled_is_rs: dict[StrategyId, list[float]] = {}
    pooled_oos_rs: dict[StrategyId, list[float]] = {}
    # Per-strategy per-window Sharpe / degradation, used for averaging
    per_strategy_window_is: dict[StrategyId, list[float]] = {}
    per_strategy_window_oos: dict[StrategyId, list[float]] = {}
    per_strategy_window_degradation: dict[StrategyId, list[float]] = {}
    per_strategy_min_trades_met: dict[StrategyId, list[bool]] = {}

    def _add_trades(
        report: BacktestReport,
    ) -> dict[StrategyId, list[StrategyTrade]]:
        buckets: dict[StrategyId, list[StrategyTrade]] = {}
        for trade in report.trades:
            buckets.setdefault(trade.strategy, []).append(trade)
        return buckets

    for window_id, (is_start, is_end, oos_end) in enumerate(triples):
        is_bars = bars[is_start:is_end]
        oos_bars = bars[is_end:oos_end]
        is_report = run_harness(
            is_bars,
            asset,
            ctx_builder=ctx_builder,
            config=cfg,
            eligibility=eligibility,
            registry=registry,
        )
        oos_report = run_harness(
            oos_bars,
            asset,
            ctx_builder=ctx_builder,
            config=cfg,
            eligibility=eligibility,
            registry=registry,
        )
        is_trades_by_strat = _add_trades(is_report)
        oos_trades_by_strat = _add_trades(oos_report)

        touched: set[StrategyId] = set(is_trades_by_strat) | set(oos_trades_by_strat)
        for sid in touched:
            is_trades = is_trades_by_strat.get(sid, [])
            oos_trades = oos_trades_by_strat.get(sid, [])
            is_rs = [t.r_multiple for t in is_trades]
            oos_rs = [t.r_multiple for t in oos_trades]

            is_sr = _sharpe_like(is_rs)
            oos_sr = _sharpe_like(oos_rs)
            is_total = sum(is_rs)
            oos_total = sum(oos_rs)
            is_n = len(is_rs)
            oos_n = len(oos_rs)
            is_hit = sum(1 for r in is_rs if r > 0.0) / is_n if is_n else 0.0
            oos_hit = sum(1 for r in oos_rs if r > 0.0) / oos_n if oos_n else 0.0
            min_met = is_n >= gate.min_trades_per_window and oos_n >= gate.min_trades_per_window
            deg = _degradation(is_sr, oos_sr)

            per_window.append(
                PerStrategyWindow(
                    strategy=sid,
                    window_id=window_id,
                    is_n_trades=is_n,
                    is_sharpe_like=is_sr,
                    is_total_r=round(is_total, 4),
                    is_hit_rate=is_hit,
                    oos_n_trades=oos_n,
                    oos_sharpe_like=oos_sr,
                    oos_total_r=round(oos_total, 4),
                    oos_hit_rate=oos_hit,
                    degradation_pct=deg,
                    min_trades_met=min_met,
                ),
            )

            pooled_is_rs.setdefault(sid, []).extend(is_rs)
            pooled_oos_rs.setdefault(sid, []).extend(oos_rs)
            per_strategy_window_is.setdefault(sid, []).append(is_sr)
            per_strategy_window_oos.setdefault(sid, []).append(oos_sr)
            per_strategy_window_degradation.setdefault(sid, []).append(deg)
            per_strategy_min_trades_met.setdefault(sid, []).append(min_met)

    # Aggregate per-strategy verdicts
    qualifications: list[StrategyQualification] = []
    n_trials = max(len(triples), 2)
    for sid in sorted(per_strategy_window_is, key=lambda s: s.value):
        is_srs = per_strategy_window_is[sid]
        oos_srs = per_strategy_window_oos[sid]
        degs = per_strategy_window_degradation[sid]
        mins = per_strategy_min_trades_met[sid]

        n_win = len(is_srs)
        avg_is = sum(is_srs) / n_win if n_win else 0.0
        avg_oos = sum(oos_srs) / n_win if n_win else 0.0
        avg_deg = sum(degs) / n_win if n_win else 0.0

        # DSR pool: OOS per-trade R multiples across every window.
        pooled_oos = pooled_oos_rs.get(sid, [])
        pooled_oos_sr = _sharpe_like(pooled_oos)
        skew, kurt = _moments(pooled_oos)
        dsr = compute_dsr(
            sharpe=pooled_oos_sr,
            n_trades=max(len(pooled_oos), 2),
            skew=skew,
            kurtosis=kurt,
            n_trials=n_trials,
        )

        fail_reasons: list[str] = []
        if dsr <= gate.dsr_threshold:
            fail_reasons.append(
                f"dsr {dsr:.4f} <= threshold {gate.dsr_threshold:.4f}",
            )
        if avg_deg >= gate.max_degradation_pct:
            fail_reasons.append(
                f"avg_degradation {avg_deg:.4f} >= max {gate.max_degradation_pct:.4f}",
            )
        if not all(mins):
            fail_reasons.append(
                f"min_trades_per_window {gate.min_trades_per_window} not met in every window",
            )

        qualifications.append(
            StrategyQualification(
                strategy=sid,
                asset=asset.upper(),
                n_windows=n_win,
                avg_is_sharpe=avg_is,
                avg_oos_sharpe=avg_oos,
                avg_degradation_pct=avg_deg,
                dsr=dsr,
                n_trades_is_total=len(pooled_is_rs.get(sid, [])),
                n_trades_oos_total=len(pooled_oos),
                passes_gate=not fail_reasons,
                fail_reasons=tuple(fail_reasons),
            ),
        )

    return QualificationReport(
        asset=asset.upper(),
        gate=gate,
        n_windows_requested=n_windows,
        n_windows_executed=len(triples),
        per_window=tuple(per_window),
        qualifications=tuple(qualifications),
        notes=tuple(notes),
    )
