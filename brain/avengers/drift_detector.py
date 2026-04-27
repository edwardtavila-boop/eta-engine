"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.drift_detector
================================================
Backtest-to-live divergence detector.

Why this exists
---------------
Every strategy that earns its way through the promotion pipeline has a
set of *backtest* daily returns attached to it from walk-forward. Once
the strategy goes LIVE (or even PAPER), its *realised* daily returns
start landing. If those realised returns diverge from the backtest
distribution beyond a threshold, the strategy is drifting -- either
the model is broken, the market regime has moved, or the backtest was
overfit. Any of those outcomes means the strategy should stop
promoting until an investigation happens.

This module does NOT decide for itself. It produces a
:class:`DriftReport` with a verdict (OK / WARN / AUTO_DEMOTE) and a
recommended :class:`PromotionAction`. The orchestrator decides whether
to act on the recommendation (usually: feed a DEMOTE into
``PromotionGate.apply()`` and push an alert).

Metrics
-------
Two complementary signals:

* **Sharpe-delta in sigma units.** Compute the standard error of the
  Sharpe ratio (``SE = sqrt((1 + 0.5*sharpe**2) / n)``) from the live
  sample, then report ``(sharpe_bt - sharpe_live) / SE``. A delta of
  1.5 sigma triggers WARN, 2.5 sigma triggers AUTO_DEMOTE.
* **KL divergence on binned returns.** Histogram both distributions
  into the same bin edges (derived from the combined range), add a
  small Laplace prior to avoid zero-division, compute
  ``KL(live || bt)``. 0.15 nats triggers WARN, 0.35 triggers
  AUTO_DEMOTE.

Both thresholds are conservative defaults. Tune per strategy family
if the overall promotion rate drops unreasonably.

Sample size guard
-----------------
Under ``min_live_samples`` realised returns, the detector refuses to
judge and returns ``verdict=OK`` with ``reasons=['insufficient live
samples']``. This prevents first-day noise from triggering auto-demote.

Stdlib-only by design -- this runs inside the live fleet hot loop so
we do not want to pull numpy/scipy.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.avengers.promotion import PromotionAction

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

DRIFT_JOURNAL: Path = Path.home() / ".jarvis" / "drift.jsonl"

TRADING_DAYS_PER_YEAR = 252


# --- schema -----------------------------------------------------------------


class DriftVerdict(StrEnum):
    OK = "OK"  # no meaningful divergence
    WARN = "WARN"  # notable but not yet actionable
    AUTO_DEMOTE = "AUTO_DEMOTE"  # divergence exceeds hard threshold


class DriftReport(BaseModel):
    """Structured output of a backtest-vs-live comparison."""

    model_config = ConfigDict(frozen=True)

    strategy_id: str
    verdict: DriftVerdict
    reasons: list[str]
    sharpe_bt: float
    sharpe_live: float
    sharpe_delta_sigma: float
    kl_divergence: float
    mean_return_delta: float
    bt_sample_size: int = Field(ge=0)
    live_sample_size: int = Field(ge=0)
    recommendation: PromotionAction | None = None
    generated_at: datetime


# --- detector ---------------------------------------------------------------


class DriftDetector:
    """Compare backtest vs live daily returns for drift.

    Parameters
    ----------
    warn_sharpe_delta_sigma
        WARN threshold on the magnitude of Sharpe delta in sigma units
        (positive means live is *worse* than backtest).
    demote_sharpe_delta_sigma
        AUTO_DEMOTE threshold on Sharpe delta in sigma units.
    warn_kl
        WARN threshold on ``KL(live || bt)`` in nats.
    demote_kl
        AUTO_DEMOTE threshold on ``KL(live || bt)`` in nats.
    min_live_samples
        Refuse to judge under this many realised returns.
    bins
        Histogram bin count for the KL-divergence estimator.
    journal_path
        Append-only audit log of every check. Defaults to
        ``~/.jarvis/drift.jsonl``.
    clock
        Injected for tests.
    """

    def __init__(
        self,
        *,
        warn_sharpe_delta_sigma: float = 1.5,
        demote_sharpe_delta_sigma: float = 2.5,
        warn_kl: float = 0.15,
        demote_kl: float = 0.35,
        min_live_samples: int = 20,
        bins: int = 10,
        journal_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if warn_sharpe_delta_sigma < 0 or demote_sharpe_delta_sigma < 0:
            msg = "sharpe-delta thresholds must be non-negative"
            raise ValueError(msg)
        if demote_sharpe_delta_sigma < warn_sharpe_delta_sigma:
            msg = "demote_sharpe_delta_sigma must be >= warn_sharpe_delta_sigma"
            raise ValueError(msg)
        if warn_kl < 0 or demote_kl < 0:
            msg = "KL thresholds must be non-negative"
            raise ValueError(msg)
        if demote_kl < warn_kl:
            msg = "demote_kl must be >= warn_kl"
            raise ValueError(msg)
        if bins < 2:
            msg = "bins must be >= 2"
            raise ValueError(msg)

        self.warn_sharpe_delta_sigma = warn_sharpe_delta_sigma
        self.demote_sharpe_delta_sigma = demote_sharpe_delta_sigma
        self.warn_kl = warn_kl
        self.demote_kl = demote_kl
        self.min_live_samples = min_live_samples
        self.bins = bins
        self.journal_path = journal_path or DRIFT_JOURNAL
        self._clock = clock or (lambda: datetime.now(UTC))

    # --- public API -------------------------------------------------------

    def check(
        self,
        strategy_id: str,
        backtest_returns: Sequence[float],
        live_returns: Sequence[float],
        *,
        journal: bool = True,
    ) -> DriftReport:
        """Compare two return series and emit a verdict.

        Never raises. Under-powered or degenerate inputs fall through to
        verdict=OK with a descriptive ``reasons`` field.
        """
        now = self._clock()
        bt = [float(x) for x in backtest_returns if math.isfinite(x)]
        lv = [float(x) for x in live_returns if math.isfinite(x)]

        # --- sample-size guard -------------------------------------------
        if len(lv) < self.min_live_samples:
            report = DriftReport(
                strategy_id=strategy_id,
                verdict=DriftVerdict.OK,
                reasons=[
                    f"insufficient live samples: have={len(lv)} need={self.min_live_samples}",
                ],
                sharpe_bt=_safe_sharpe(bt),
                sharpe_live=_safe_sharpe(lv),
                sharpe_delta_sigma=0.0,
                kl_divergence=0.0,
                mean_return_delta=0.0,
                bt_sample_size=len(bt),
                live_sample_size=len(lv),
                recommendation=None,
                generated_at=now,
            )
            if journal:
                self._journal(report)
            return report

        if len(bt) < 2:
            report = DriftReport(
                strategy_id=strategy_id,
                verdict=DriftVerdict.OK,
                reasons=[
                    f"insufficient backtest samples: have={len(bt)} need>=2",
                ],
                sharpe_bt=0.0,
                sharpe_live=_safe_sharpe(lv),
                sharpe_delta_sigma=0.0,
                kl_divergence=0.0,
                mean_return_delta=0.0,
                bt_sample_size=len(bt),
                live_sample_size=len(lv),
                recommendation=None,
                generated_at=now,
            )
            if journal:
                self._journal(report)
            return report

        # --- sharpe delta -------------------------------------------------
        sh_bt, se_bt = _sharpe_with_se(bt)
        sh_live, se_live = _sharpe_with_se(lv)
        # Both estimators are noisy. The SE of the difference combines
        # the per-sample Lo-(2002) SEs in quadrature. With n_bt >> n_lv
        # the combined SE collapses to ~se_live, which matches the
        # intuition that the backtest is known more precisely. With
        # comparable sample sizes we get the correct widening.
        se_delta = math.sqrt(se_bt * se_bt + se_live * se_live)
        delta_sigma = abs(sh_bt - sh_live) / se_delta if se_delta > 0 else 0.0
        mean_delta = statistics.fmean(lv) - statistics.fmean(bt)

        # --- KL divergence ------------------------------------------------
        kl = _kl_divergence(lv, bt, bins=self.bins)

        # --- verdict ------------------------------------------------------
        reasons: list[str] = []
        verdict = DriftVerdict.OK

        if delta_sigma >= self.demote_sharpe_delta_sigma:
            verdict = DriftVerdict.AUTO_DEMOTE
            reasons.append(
                f"sharpe_delta={delta_sigma:.2f}sigma >= demote={self.demote_sharpe_delta_sigma:.2f}sigma",
            )
        elif delta_sigma >= self.warn_sharpe_delta_sigma:
            verdict = _escalate(verdict, DriftVerdict.WARN)
            reasons.append(
                f"sharpe_delta={delta_sigma:.2f}sigma >= warn={self.warn_sharpe_delta_sigma:.2f}sigma",
            )

        if kl >= self.demote_kl:
            verdict = DriftVerdict.AUTO_DEMOTE
            reasons.append(f"KL={kl:.3f} >= demote={self.demote_kl:.3f}")
        elif kl >= self.warn_kl:
            verdict = _escalate(verdict, DriftVerdict.WARN)
            reasons.append(f"KL={kl:.3f} >= warn={self.warn_kl:.3f}")

        if not reasons:
            reasons.append(
                f"sharpe_delta={delta_sigma:.2f}sigma (ok) KL={kl:.3f} (ok)",
            )

        recommendation = PromotionAction.DEMOTE if verdict is DriftVerdict.AUTO_DEMOTE else None

        report = DriftReport(
            strategy_id=strategy_id,
            verdict=verdict,
            reasons=reasons,
            sharpe_bt=sh_bt,
            sharpe_live=sh_live,
            sharpe_delta_sigma=delta_sigma,
            kl_divergence=kl,
            mean_return_delta=mean_delta,
            bt_sample_size=len(bt),
            live_sample_size=len(lv),
            recommendation=recommendation,
            generated_at=now,
        )
        if journal:
            self._journal(report)
        return report

    # --- journaling -------------------------------------------------------

    def _journal(self, report: DriftReport) -> None:
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(report.model_dump_json() + "\n")
        except OSError:
            return


# --- helpers ----------------------------------------------------------------


def _safe_sharpe(returns: Sequence[float]) -> float:
    """Annualised Sharpe ratio from daily returns. 0.0 on degenerate input."""
    sharpe, _ = _sharpe_with_se(returns)
    return sharpe


def _sharpe_with_se(returns: Sequence[float]) -> tuple[float, float]:
    """Return ``(annualised_sharpe, SE)`` for a daily-return series.

    Uses sample stdev (divisor ``n-1``) which is the standard choice for
    finite-sample Sharpe. SE follows Lo (2002) asymptotic formula:
    ``SE ~= sqrt((1 + 0.5 * SR^2) / n)``. Autocorrelation is ignored --
    the correction term matters for HF data with strong
    autocorrelation but is small for daily return series.
    """
    xs = [float(r) for r in returns if math.isfinite(r)]
    if len(xs) < 2:
        return 0.0, math.inf
    mean = statistics.fmean(xs)
    stdev = statistics.stdev(xs)
    if stdev <= 0.0:
        return 0.0, math.inf
    sharpe = mean * math.sqrt(TRADING_DAYS_PER_YEAR) / stdev
    se = math.sqrt((1.0 + 0.5 * sharpe * sharpe) / len(xs))
    return sharpe, se


def _kl_divergence(
    live: Sequence[float],
    backtest: Sequence[float],
    *,
    bins: int = 10,
    prior: float = 1.0,
) -> float:
    """KL divergence ``KL(live || backtest)`` on a shared histogram.

    Uses Laplace smoothing so empty bins do not explode to infinity.
    Returns 0.0 if either input is too small to histogram.
    """
    if len(live) < 2 or len(backtest) < 2:
        return 0.0

    lo = min(min(live), min(backtest))
    hi = max(max(live), max(backtest))
    if hi <= lo:
        return 0.0

    edges = [lo + (hi - lo) * i / bins for i in range(bins + 1)]

    def _hist(xs: Sequence[float]) -> list[float]:
        counts = [prior] * bins  # Laplace prior
        for x in xs:
            if x >= edges[-1]:
                counts[-1] += 1
                continue
            # Linear scan is fine for small bin counts; keeps stdlib-only.
            for i in range(bins):
                if edges[i] <= x < edges[i + 1]:
                    counts[i] += 1
                    break
        total = sum(counts)
        return [c / total for c in counts]

    p = _hist(live)
    q = _hist(backtest)
    kl = 0.0
    for pi, qi in zip(p, q, strict=True):
        if pi <= 0.0 or qi <= 0.0:
            continue
        kl += pi * math.log(pi / qi)
    return max(0.0, kl)


def _escalate(current: DriftVerdict, proposed: DriftVerdict) -> DriftVerdict:
    """Keep the worst verdict; never downgrade."""
    rank = {DriftVerdict.OK: 0, DriftVerdict.WARN: 1, DriftVerdict.AUTO_DEMOTE: 2}
    return proposed if rank[proposed] > rank[current] else current


# --- journal readback ------------------------------------------------------


def read_drift_journal(
    path: Path | None = None,
    *,
    n: int = 50,
) -> list[dict]:
    """Tail the drift journal. Returns last ``n`` records (best-effort)."""
    p = path or DRIFT_JOURNAL
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for raw in lines[-n:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


__all__ = [
    "DRIFT_JOURNAL",
    "DriftDetector",
    "DriftReport",
    "DriftVerdict",
    "read_drift_journal",
]
