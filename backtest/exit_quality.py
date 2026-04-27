"""
EVOLUTIONARY TRADING ALGO  //  backtest.exit_quality
========================================
MAE / MFE based exit-quality analyzer.

Why this exists
---------------
A 2R winner that walked all the way to 4R and came back is not a win --
it's a leak. The trade_grader scores one trade; this module slices a
whole population of closed trades by regime and setup, producing:

  * Per-trade: ExitEfficiency (fraction of available R captured),
    CaptureRatio, TimeInTradeR, HoldScore.
  * Aggregated: heatmap by (regime, setup) with mean efficiency,
    mean R captured, and "money left on table" in dollar terms.

Design
------
Zero IO. Pure functions over pydantic-typed inputs. Caller loads trades
from the event journal / trade grader pipeline / CSV and feeds them in.

Public API
----------
  * ``MaeMfePoint`` -- one trade's MAE/MFE snapshot
  * ``ExitQualityRow`` -- per-trade derived metrics
  * ``ExitQualityHeatmap`` -- aggregated by (regime, setup)
  * ``analyze_trade(point) -> ExitQualityRow``
  * ``analyze_batch(points) -> list[ExitQualityRow]``
  * ``build_heatmap(rows) -> dict[(regime, setup), ExitQualityHeatmap]``
  * ``money_left_on_table(rows, dollars_per_r) -> float``
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  -- pydantic needs it at runtime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Enums -- kept local to avoid circular deps with brain/regime
# ---------------------------------------------------------------------------


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class MaeMfePoint(BaseModel):
    """One closed trade with its MAE/MFE trace collapsed to peaks."""

    trade_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side
    regime: str = Field(min_length=1)
    setup: str = Field(
        min_length=1,
        description="Setup label: 'breakout_retest', 'fade_open', ...",
    )
    opened_at: datetime
    closed_at: datetime

    entry_price: float = Field(gt=0.0)
    exit_price: float = Field(gt=0.0)
    stop_price: float = Field(gt=0.0)

    mfe_price: float = Field(gt=0.0)
    mae_price: float = Field(gt=0.0)
    time_to_mfe_sec: float = Field(
        ge=0.0,
        description="Seconds from entry to the MFE peak.",
    )

    @model_validator(mode="after")
    def _check_time(self) -> MaeMfePoint:
        if self.closed_at <= self.opened_at:
            raise ValueError("closed_at must be after opened_at")
        return self

    # -- derived R-metrics --------------------------------------------------

    @property
    def r_risk(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @property
    def r_captured(self) -> float:
        if self.r_risk == 0.0:
            return 0.0
        signed = self.exit_price - self.entry_price
        if self.side == Side.SHORT:
            signed = -signed
        return signed / self.r_risk

    @property
    def r_available(self) -> float:
        if self.r_risk == 0.0:
            return 0.0
        if self.side == Side.LONG:
            excursion = max(0.0, self.mfe_price - self.entry_price)
        else:
            excursion = max(0.0, self.entry_price - self.mfe_price)
        return excursion / self.r_risk

    @property
    def r_adverse(self) -> float:
        """Max adverse excursion expressed in R (always positive)."""
        if self.r_risk == 0.0:
            return 0.0
        if self.side == Side.LONG:
            excursion = max(0.0, self.entry_price - self.mae_price)
        else:
            excursion = max(0.0, self.mae_price - self.entry_price)
        return excursion / self.r_risk

    @property
    def hold_seconds(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds()


class ExitQualityRow(BaseModel):
    trade_id: str
    regime: str
    setup: str
    r_captured: float
    r_available: float
    r_adverse: float
    capture_ratio: float = Field(
        ge=0.0,
        le=1.0,
        description="min(max(captured/available, 0), 1); 1.0 = perfect exit.",
    )
    hold_frac_to_mfe: float = Field(
        ge=0.0,
        le=1.0,
        description="Fraction of hold time spent BEFORE the MFE peak.",
    )
    hold_score: float = Field(
        ge=0.0,
        le=1.0,
        description="(1 - capture_ratio) inverted -> longer hold past MFE = lower",
    )
    leaked_r: float = Field(
        description="R left on the table (r_available - r_captured). Negative = stop-out (no leak).",
    )


class ExitQualityHeatmap(BaseModel):
    regime: str
    setup: str
    n: int = Field(ge=0)
    mean_capture_ratio: float
    mean_r_captured: float
    mean_r_available: float
    mean_leaked_r: float
    worst_leak_r: float
    best_capture_ratio: float


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze_trade(p: MaeMfePoint) -> ExitQualityRow:
    cap_raw = p.r_captured / p.r_available if p.r_available > 0 else (1.0 if p.r_captured >= 0 else 0.0)
    capture_ratio = max(0.0, min(1.0, cap_raw))

    hold_s = p.hold_seconds
    hold_frac = max(0.0, min(1.0, p.time_to_mfe_sec / hold_s)) if hold_s > 0 else 1.0

    # If you peaked 20% into the hold and slid 80% more -> hold_score low
    # If you exited right at the peak -> hold_score high
    hold_score = 1.0 - max(0.0, 1.0 - capture_ratio) * max(0.0, 1.0 - hold_frac)
    hold_score = max(0.0, min(1.0, hold_score))

    leaked = p.r_available - p.r_captured

    return ExitQualityRow(
        trade_id=p.trade_id,
        regime=p.regime,
        setup=p.setup,
        r_captured=round(p.r_captured, 3),
        r_available=round(p.r_available, 3),
        r_adverse=round(p.r_adverse, 3),
        capture_ratio=round(capture_ratio, 3),
        hold_frac_to_mfe=round(hold_frac, 3),
        hold_score=round(hold_score, 3),
        leaked_r=round(leaked, 3),
    )


def analyze_batch(points: list[MaeMfePoint]) -> list[ExitQualityRow]:
    return [analyze_trade(p) for p in points]


def build_heatmap(
    rows: list[ExitQualityRow],
) -> dict[tuple[str, str], ExitQualityHeatmap]:
    """Aggregate rows by (regime, setup). Only rows with r_available > 0
    contribute to mean_capture_ratio; straight stop-outs are still counted
    for n and leaked_r (where leaked_r is 0 in those cases).
    """
    grouped: dict[tuple[str, str], list[ExitQualityRow]] = {}
    for row in rows:
        grouped.setdefault((row.regime, row.setup), []).append(row)

    out: dict[tuple[str, str], ExitQualityHeatmap] = {}
    for (regime, setup), group in grouped.items():
        n = len(group)
        # Trades with r_available > 0 are the only ones where capture is meaningful
        meaningful = [r for r in group if r.r_available > 0]
        mean_cap = sum(r.capture_ratio for r in meaningful) / len(meaningful) if meaningful else 0.0
        best_cap = max((r.capture_ratio for r in meaningful), default=0.0)
        mean_r_captured = sum(r.r_captured for r in group) / n
        mean_r_available = sum(r.r_available for r in group) / n
        leaks = [r.leaked_r for r in group]
        mean_leaked = sum(leaks) / n
        worst_leak = max(leaks) if leaks else 0.0

        out[(regime, setup)] = ExitQualityHeatmap(
            regime=regime,
            setup=setup,
            n=n,
            mean_capture_ratio=round(mean_cap, 3),
            mean_r_captured=round(mean_r_captured, 3),
            mean_r_available=round(mean_r_available, 3),
            mean_leaked_r=round(mean_leaked, 3),
            worst_leak_r=round(worst_leak, 3),
            best_capture_ratio=round(best_cap, 3),
        )
    return out


def money_left_on_table(
    rows: list[ExitQualityRow],
    *,
    dollars_per_r: float,
) -> float:
    """Total $ that better exits would have harvested, across the batch.

    Negative leaked_r (stop-out) contributes zero.
    """
    if dollars_per_r <= 0:
        raise ValueError("dollars_per_r must be > 0")
    total_r = sum(max(0.0, r.leaked_r) for r in rows)
    return round(total_r * dollars_per_r, 2)


def rank_setups_by_leak(
    rows: list[ExitQualityRow],
) -> list[tuple[str, float]]:
    """Return setups ranked by total leaked R (descending)."""
    by_setup: dict[str, float] = {}
    for r in rows:
        if r.leaked_r > 0:
            by_setup[r.setup] = by_setup.get(r.setup, 0.0) + r.leaked_r
    return sorted(by_setup.items(), key=lambda x: x[1], reverse=True)
