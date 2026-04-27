"""
EVOLUTIONARY TRADING ALGO  //  core.trade_grader
====================================
Post-trade A+ grader. Every closed trade gets a 0-100 score.

Why this exists
---------------
The narrative behind the whole system: "A+ trade" is a question about
process, not outcome. A trade that hit its target but was pulled from
the wrong regime with a 3/10 confluence score is NOT an A+ trade. A
trade that stopped out cleanly, in the right regime, with high confluence
and perfect exit efficiency, IS. This module implements that distinction.

Scoring rubric (100 pts total)
------------------------------
  * Entry timing (20 pts) -- how much of the MFE was captured before
    the first adverse pullback >= 0.5R.
  * Regime fit (20 pts) -- was the regime-axis prediction correct for
    the trade direction (long in TRENDING_UP, short in TRENDING_DOWN,
    short in CRISIS, etc.)?
  * Confluence accuracy (20 pts) -- pre-trade score vs outcome R.
    High confluence + winning trade = full marks. Low confluence +
    winning trade = lucky, partial marks. High confluence + losing
    trade = process-correct but unlucky, partial marks. Low confluence
    + losing trade = worst case, zero marks.
  * Exit efficiency (20 pts) -- R-captured / R-available, where R
    is defined off the initial stop distance.
  * Rule adherence (20 pts) -- starts at 20, deducted 5 pts per
    gate override recorded during the trade lifetime.

Anything scoring >= 85 is A+.
Anything scoring < 60 is a "leak" and goes on the weekly review.

Public API
----------
  * ``TradeSide`` StrEnum
  * ``TradeRegime`` StrEnum (matches brain.regime but kept local to
    avoid circular imports)
  * ``ClosedTrade`` pydantic model -- input
  * ``GradeBreakdown`` -- per-component scores
  * ``TradeGrade`` -- final with letter
  * ``TradeGrader`` class with ``grade(trade) -> TradeGrade``
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TradeSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class TradeRegime(StrEnum):
    """Regime labels used for scoring. Match brain.regime.RegimeType."""

    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    HIGH_VOL = "HIGH_VOL"
    LOW_VOL = "LOW_VOL"
    CRISIS = "CRISIS"
    TRANSITION = "TRANSITION"


class Letter(StrEnum):
    A_PLUS = "A+"
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ClosedTrade(BaseModel):
    """Everything the grader needs to know about one completed trade."""

    trade_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: TradeSide
    opened_at: datetime
    closed_at: datetime
    entry_price: float = Field(gt=0.0)
    exit_price: float = Field(gt=0.0)
    stop_price: float = Field(
        gt=0.0,
        description="Initial protective stop placed at entry time.",
    )
    target_price: float | None = Field(
        default=None,
        gt=0.0,
        description="Optional first-target; used only for reporting.",
    )
    mfe_price: float = Field(
        gt=0.0,
        description="Max-favorable excursion price during trade life.",
    )
    mae_price: float = Field(
        gt=0.0,
        description="Max-adverse excursion price during trade life.",
    )
    first_pullback_frac: float = Field(
        ge=0.0,
        le=1.0,
        description="Fraction of MFE captured before first adverse pullback >= 0.5R",
    )
    confluence_score: float = Field(
        ge=0.0,
        le=10.0,
        description="Pre-trade confluence score (0-10 from confluence_scorer).",
    )
    regime_at_entry: TradeRegime
    gate_overrides: int = Field(
        default=0,
        ge=0,
        description="Number of times an operator overrode a gate for this trade.",
    )

    @model_validator(mode="after")
    def _check_time_order(self) -> ClosedTrade:
        if self.closed_at <= self.opened_at:
            raise ValueError("closed_at must be after opened_at")
        return self

    @property
    def r_risk(self) -> float:
        """Dollar-distance risked per contract (|entry - stop|)."""
        return abs(self.entry_price - self.stop_price)

    @property
    def r_captured(self) -> float:
        """R-multiple actually captured (signed). Positive = winner."""
        if self.r_risk == 0.0:
            return 0.0
        signed = self.exit_price - self.entry_price
        if self.side == TradeSide.SHORT:
            signed = -signed
        return signed / self.r_risk

    @property
    def r_available(self) -> float:
        """R-multiple that WAS available (peak MFE / risk)."""
        if self.r_risk == 0.0:
            return 0.0
        if self.side == TradeSide.LONG:
            excursion = max(0.0, self.mfe_price - self.entry_price)
        else:
            excursion = max(0.0, self.entry_price - self.mfe_price)
        return excursion / self.r_risk


class GradeBreakdown(BaseModel):
    entry_timing: float = Field(ge=0.0, le=20.0)
    regime_fit: float = Field(ge=0.0, le=20.0)
    confluence_accuracy: float = Field(ge=0.0, le=20.0)
    exit_efficiency: float = Field(ge=0.0, le=20.0)
    rule_adherence: float = Field(ge=0.0, le=20.0)

    @property
    def total(self) -> float:
        return round(
            self.entry_timing + self.regime_fit + self.confluence_accuracy + self.exit_efficiency + self.rule_adherence,
            2,
        )


class TradeGrade(BaseModel):
    trade_id: str
    graded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    breakdown: GradeBreakdown
    total: float
    letter: Letter
    r_captured: float
    r_available: float
    is_winner: bool
    leaks: list[str] = Field(
        default_factory=list,
        description="Human-readable notes about what dragged the score down.",
    )


# ---------------------------------------------------------------------------
# Grader
# ---------------------------------------------------------------------------


class TradeGrader:
    """Stateless grader. Call ``grade(trade)`` to produce a TradeGrade."""

    LETTER_BANDS = [
        (Letter.A_PLUS, 85.0),
        (Letter.A, 75.0),
        (Letter.B, 65.0),
        (Letter.C, 55.0),
        (Letter.D, 45.0),
        (Letter.F, 0.0),
    ]

    # --- top-level --------------------------------------------------------

    def grade(self, trade: ClosedTrade) -> TradeGrade:
        leaks: list[str] = []

        entry_timing = self._score_entry_timing(trade, leaks)
        regime_fit = self._score_regime_fit(trade, leaks)
        confluence_accuracy = self._score_confluence_accuracy(trade, leaks)
        exit_efficiency = self._score_exit_efficiency(trade, leaks)
        rule_adherence = self._score_rule_adherence(trade, leaks)

        breakdown = GradeBreakdown(
            entry_timing=entry_timing,
            regime_fit=regime_fit,
            confluence_accuracy=confluence_accuracy,
            exit_efficiency=exit_efficiency,
            rule_adherence=rule_adherence,
        )
        total = breakdown.total
        letter = self._assign_letter(total)

        return TradeGrade(
            trade_id=trade.trade_id,
            breakdown=breakdown,
            total=total,
            letter=letter,
            r_captured=round(trade.r_captured, 3),
            r_available=round(trade.r_available, 3),
            is_winner=trade.r_captured > 0,
            leaks=leaks,
        )

    # --- components -------------------------------------------------------

    def _score_entry_timing(
        self,
        trade: ClosedTrade,
        leaks: list[str],
    ) -> float:
        """20 pts * first_pullback_frac."""
        pts = 20.0 * trade.first_pullback_frac
        if trade.first_pullback_frac < 0.3:
            leaks.append(
                f"entry_timing: gave back {(1 - trade.first_pullback_frac):.0%} of MFE before moving in your favor",
            )
        return round(pts, 2)

    def _score_regime_fit(
        self,
        trade: ClosedTrade,
        leaks: list[str],
    ) -> float:
        r = trade.regime_at_entry
        side = trade.side
        good_pairs = {
            (TradeRegime.TRENDING_UP, TradeSide.LONG),
            (TradeRegime.TRENDING_DOWN, TradeSide.SHORT),
            (TradeRegime.CRISIS, TradeSide.SHORT),
            (TradeRegime.RANGING, TradeSide.LONG),
            (TradeRegime.RANGING, TradeSide.SHORT),
            (TradeRegime.LOW_VOL, TradeSide.LONG),
            (TradeRegime.LOW_VOL, TradeSide.SHORT),
        }
        bad_pairs = {
            (TradeRegime.TRENDING_UP, TradeSide.SHORT),
            (TradeRegime.TRENDING_DOWN, TradeSide.LONG),
            (TradeRegime.CRISIS, TradeSide.LONG),
        }
        if (r, side) in bad_pairs:
            leaks.append(
                f"regime_fit: {side.value} in {r.value} is a counter-trend fight",
            )
            return 0.0
        if (r, side) in good_pairs:
            return 20.0
        # HIGH_VOL and TRANSITION -> partial; trades are allowed but discounted
        leaks.append(
            f"regime_fit: {r.value} is a low-conviction regime for either side",
        )
        return 10.0

    def _score_confluence_accuracy(
        self,
        trade: ClosedTrade,
        leaks: list[str],
    ) -> float:
        """20 pts iff score and outcome align.

        High-score winner or low-score loser = process correct = full marks.
        High-score loser = bad luck = 12 pts.
        Low-score winner = lucky = 8 pts.
        Low-score loser = lucky-bad + dumb = 0 pts.
        """
        high = trade.confluence_score >= 7.0
        winner = trade.r_captured > 0
        if high and winner:
            return 20.0
        if not high and not winner:
            return 20.0  # process was right (low score -> no trade, but if taken, loss expected)
        if high and not winner:
            leaks.append(
                f"confluence_accuracy: 8/10 signal took a loss "
                f"(R={trade.r_captured:.2f}) -- unlucky but process-correct",
            )
            return 12.0
        # low + winner
        leaks.append(
            f"confluence_accuracy: only {trade.confluence_score:.1f}/10 signal "
            f"but won (R={trade.r_captured:.2f}) -- lucky, don't count on this",
        )
        return 8.0

    def _score_exit_efficiency(
        self,
        trade: ClosedTrade,
        leaks: list[str],
    ) -> float:
        """20 pts * (R captured / R available), clipped at 0..20."""
        if trade.r_available <= 0:
            # No favorable excursion -- either a straight stop-out (not the
            # grader's job to judge) or an extremely unusual fill. No penalty.
            return 20.0 if trade.r_captured <= 0 else 20.0
        ratio = trade.r_captured / trade.r_available
        ratio = max(0.0, min(1.0, ratio))
        pts = 20.0 * ratio
        if ratio < 0.5:
            leaks.append(
                f"exit_efficiency: captured {ratio:.0%} of {trade.r_available:.2f}R available",
            )
        return round(pts, 2)

    def _score_rule_adherence(
        self,
        trade: ClosedTrade,
        leaks: list[str],
    ) -> float:
        pts = 20.0 - 5.0 * trade.gate_overrides
        pts = max(0.0, pts)
        if trade.gate_overrides > 0:
            leaks.append(
                f"rule_adherence: {trade.gate_overrides} gate override(s) during trade lifetime",
            )
        return pts

    # --- letter -----------------------------------------------------------

    @classmethod
    def _assign_letter(cls, total: float) -> Letter:
        for letter, threshold in cls.LETTER_BANDS:
            if total >= threshold:
                return letter
        return Letter.F


# ---------------------------------------------------------------------------
# Convenience -- batch grading + aggregate
# ---------------------------------------------------------------------------


def grade_many(trades: list[ClosedTrade]) -> list[TradeGrade]:
    g = TradeGrader()
    return [g.grade(t) for t in trades]


def leak_distribution(grades: list[TradeGrade]) -> dict[str, int]:
    """Count which leak categories fire most often across a batch."""
    counts: dict[str, int] = {}
    for g in grades:
        for leak in g.leaks:
            # key = the part before the first colon
            key = leak.split(":", 1)[0]
            counts[key] = counts.get(key, 0) + 1
    return counts
