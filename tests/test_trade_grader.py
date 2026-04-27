"""Tests for core.trade_grader -- post-trade A+ grading."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from eta_engine.core.trade_grader import (
    ClosedTrade,
    GradeBreakdown,
    Letter,
    TradeGrader,
    TradeRegime,
    TradeSide,
    grade_many,
    leak_distribution,
)

_T0 = datetime(2025, 1, 1, 9, 30, tzinfo=UTC)
_T1 = _T0 + timedelta(minutes=15)


def _trade(
    *,
    side: TradeSide = TradeSide.LONG,
    entry: float = 17_000.0,
    exit_: float = 17_010.0,
    stop: float = 16_990.0,
    mfe: float = 17_015.0,
    mae: float = 16_995.0,
    first_pullback: float = 0.8,
    confluence: float = 8.0,
    regime: TradeRegime = TradeRegime.TRENDING_UP,
    overrides: int = 0,
    trade_id: str = "t-1",
) -> ClosedTrade:
    return ClosedTrade(
        trade_id=trade_id,
        symbol="MNQ",
        side=side,
        opened_at=_T0,
        closed_at=_T1,
        entry_price=entry,
        exit_price=exit_,
        stop_price=stop,
        mfe_price=mfe,
        mae_price=mae,
        first_pullback_frac=first_pullback,
        confluence_score=confluence,
        regime_at_entry=regime,
        gate_overrides=overrides,
    )


# --------------------------------------------------------------------------- #
# ClosedTrade validation
# --------------------------------------------------------------------------- #


def test_closed_at_must_be_after_opened_at() -> None:
    with pytest.raises(ValidationError):
        ClosedTrade(
            trade_id="x",
            symbol="MNQ",
            side=TradeSide.LONG,
            opened_at=_T1,
            closed_at=_T0,
            entry_price=1.0,
            exit_price=1.0,
            stop_price=0.5,
            mfe_price=1.1,
            mae_price=0.9,
            first_pullback_frac=0.5,
            confluence_score=5.0,
            regime_at_entry=TradeRegime.RANGING,
        )


def test_first_pullback_frac_bounded() -> None:
    with pytest.raises(ValidationError):
        _trade(first_pullback=1.5)


def test_confluence_score_bounded() -> None:
    with pytest.raises(ValidationError):
        _trade(confluence=15.0)


def test_negative_overrides_rejected() -> None:
    with pytest.raises(ValidationError):
        _trade(overrides=-1)


# --------------------------------------------------------------------------- #
# R-metrics
# --------------------------------------------------------------------------- #


def test_r_risk_long() -> None:
    t = _trade(entry=100.0, stop=95.0)
    assert t.r_risk == 5.0


def test_r_risk_short() -> None:
    t = _trade(side=TradeSide.SHORT, entry=100.0, stop=105.0, exit_=95.0, mfe=94.0, mae=101.0)
    assert t.r_risk == 5.0


def test_r_captured_long_winner() -> None:
    t = _trade(entry=100.0, exit_=110.0, stop=95.0)
    assert t.r_captured == 2.0  # 10 / 5 = 2R


def test_r_captured_long_loser() -> None:
    t = _trade(entry=100.0, exit_=97.5, stop=95.0, mfe=101.0)
    assert t.r_captured == -0.5


def test_r_captured_short_winner() -> None:
    t = _trade(side=TradeSide.SHORT, entry=100.0, exit_=90.0, stop=105.0, mfe=89.0, mae=101.0)
    assert t.r_captured == 2.0


def test_r_available_matches_mfe() -> None:
    t = _trade(entry=100.0, exit_=105.0, stop=95.0, mfe=115.0)
    # mfe excursion = 15, r_risk = 5 -> 3R available
    assert t.r_available == 3.0


def test_r_available_zero_if_mfe_not_favorable() -> None:
    t = _trade(entry=100.0, exit_=97.0, stop=95.0, mfe=99.0)
    # mfe never went above entry on the long side
    assert t.r_available == 0.0


# --------------------------------------------------------------------------- #
# Entry timing
# --------------------------------------------------------------------------- #


def test_entry_timing_full_marks_at_1_0() -> None:
    g = TradeGrader()
    tr = _trade(first_pullback=1.0)
    gr = g.grade(tr)
    assert gr.breakdown.entry_timing == 20.0


def test_entry_timing_zero_at_0_0() -> None:
    g = TradeGrader()
    tr = _trade(first_pullback=0.0)
    gr = g.grade(tr)
    assert gr.breakdown.entry_timing == 0.0
    assert any("entry_timing" in leak for leak in gr.leaks)


def test_entry_timing_partial() -> None:
    g = TradeGrader()
    tr = _trade(first_pullback=0.5)
    gr = g.grade(tr)
    assert gr.breakdown.entry_timing == 10.0


# --------------------------------------------------------------------------- #
# Regime fit
# --------------------------------------------------------------------------- #


def test_regime_fit_long_trending_up() -> None:
    g = TradeGrader()
    gr = g.grade(_trade(regime=TradeRegime.TRENDING_UP, side=TradeSide.LONG))
    assert gr.breakdown.regime_fit == 20.0


def test_regime_fit_short_trending_down() -> None:
    g = TradeGrader()
    tr = _trade(
        side=TradeSide.SHORT,
        regime=TradeRegime.TRENDING_DOWN,
        entry=100.0,
        exit_=90.0,
        stop=105.0,
        mfe=89.0,
        mae=101.0,
    )
    gr = g.grade(tr)
    assert gr.breakdown.regime_fit == 20.0


def test_regime_fit_long_trending_down_is_counter() -> None:
    g = TradeGrader()
    tr = _trade(regime=TradeRegime.TRENDING_DOWN, side=TradeSide.LONG)
    gr = g.grade(tr)
    assert gr.breakdown.regime_fit == 0.0
    assert any("regime_fit" in leak for leak in gr.leaks)


def test_regime_fit_long_in_crisis_is_zero() -> None:
    g = TradeGrader()
    tr = _trade(regime=TradeRegime.CRISIS, side=TradeSide.LONG)
    gr = g.grade(tr)
    assert gr.breakdown.regime_fit == 0.0


def test_regime_fit_high_vol_is_half() -> None:
    g = TradeGrader()
    tr = _trade(regime=TradeRegime.HIGH_VOL)
    gr = g.grade(tr)
    assert gr.breakdown.regime_fit == 10.0


def test_regime_fit_transition_is_half() -> None:
    g = TradeGrader()
    tr = _trade(regime=TradeRegime.TRANSITION)
    gr = g.grade(tr)
    assert gr.breakdown.regime_fit == 10.0


# --------------------------------------------------------------------------- #
# Confluence accuracy
# --------------------------------------------------------------------------- #


def test_confluence_accuracy_high_winner_full_marks() -> None:
    g = TradeGrader()
    tr = _trade(confluence=9.0, entry=100.0, exit_=110.0, stop=95.0)
    gr = g.grade(tr)
    assert gr.breakdown.confluence_accuracy == 20.0


def test_confluence_accuracy_low_loser_full_marks() -> None:
    g = TradeGrader()
    tr = _trade(confluence=3.0, entry=100.0, exit_=97.0, stop=95.0, mfe=101.0)
    gr = g.grade(tr)
    assert gr.breakdown.confluence_accuracy == 20.0


def test_confluence_accuracy_high_loser_partial() -> None:
    g = TradeGrader()
    tr = _trade(confluence=9.0, entry=100.0, exit_=97.0, stop=95.0, mfe=101.0)
    gr = g.grade(tr)
    assert gr.breakdown.confluence_accuracy == 12.0


def test_confluence_accuracy_low_winner_penalized() -> None:
    g = TradeGrader()
    tr = _trade(confluence=3.0, entry=100.0, exit_=110.0, stop=95.0)
    gr = g.grade(tr)
    assert gr.breakdown.confluence_accuracy == 8.0
    assert any("lucky" in leak for leak in gr.leaks)


# --------------------------------------------------------------------------- #
# Exit efficiency
# --------------------------------------------------------------------------- #


def test_exit_efficiency_full_marks_when_captured_equals_available() -> None:
    g = TradeGrader()
    tr = _trade(entry=100.0, exit_=110.0, stop=95.0, mfe=110.0)
    gr = g.grade(tr)
    assert gr.breakdown.exit_efficiency == 20.0


def test_exit_efficiency_half_marks_when_captured_half() -> None:
    g = TradeGrader()
    # available = 3R, captured = 1R -> 33% -> ~6.67 pts
    tr = _trade(entry=100.0, exit_=105.0, stop=95.0, mfe=115.0)
    gr = g.grade(tr)
    assert gr.breakdown.exit_efficiency == pytest.approx(6.67, abs=0.01)
    assert any("exit_efficiency" in leak for leak in gr.leaks)


def test_exit_efficiency_clipped_to_zero_on_loss() -> None:
    g = TradeGrader()
    tr = _trade(entry=100.0, exit_=97.0, stop=95.0, mfe=102.0)
    gr = g.grade(tr)
    # captured = -3/5 = -0.6 -> clipped to 0
    assert gr.breakdown.exit_efficiency == 0.0


# --------------------------------------------------------------------------- #
# Rule adherence
# --------------------------------------------------------------------------- #


def test_rule_adherence_full_marks_with_no_overrides() -> None:
    g = TradeGrader()
    gr = g.grade(_trade(overrides=0))
    assert gr.breakdown.rule_adherence == 20.0


def test_rule_adherence_deducts_5_per_override() -> None:
    g = TradeGrader()
    for overrides, expected in [(1, 15.0), (2, 10.0), (3, 5.0), (4, 0.0)]:
        gr = g.grade(_trade(overrides=overrides))
        assert gr.breakdown.rule_adherence == expected


def test_rule_adherence_never_negative() -> None:
    g = TradeGrader()
    gr = g.grade(_trade(overrides=10))
    assert gr.breakdown.rule_adherence == 0.0


# --------------------------------------------------------------------------- #
# Letter + total
# --------------------------------------------------------------------------- #


def test_a_plus_when_every_component_maxed() -> None:
    g = TradeGrader()
    tr = _trade(
        entry=100.0,
        exit_=110.0,
        stop=95.0,
        mfe=110.0,
        first_pullback=1.0,
        confluence=9.0,
        regime=TradeRegime.TRENDING_UP,
        side=TradeSide.LONG,
        overrides=0,
    )
    gr = g.grade(tr)
    assert gr.total == 100.0
    assert gr.letter == Letter.A_PLUS


def test_f_when_everything_wrong() -> None:
    g = TradeGrader()
    tr = _trade(
        entry=100.0,
        exit_=97.0,
        stop=95.0,
        mfe=101.0,
        first_pullback=0.0,
        confluence=3.0,
        regime=TradeRegime.TRENDING_DOWN,
        side=TradeSide.LONG,
        overrides=4,
    )
    gr = g.grade(tr)
    assert gr.total < 45.0
    assert gr.letter == Letter.F


def test_letter_bands() -> None:
    # Check each threshold boundary
    tester = TradeGrader()
    for total, letter in [
        (90.0, Letter.A_PLUS),
        (85.0, Letter.A_PLUS),
        (80.0, Letter.A),
        (75.0, Letter.A),
        (70.0, Letter.B),
        (60.0, Letter.C),
        (50.0, Letter.D),
        (40.0, Letter.F),
        (0.0, Letter.F),
    ]:
        assert tester._assign_letter(total) == letter


# --------------------------------------------------------------------------- #
# Batch + distribution
# --------------------------------------------------------------------------- #


def test_grade_many() -> None:
    trades = [_trade(trade_id=f"t-{i}") for i in range(4)]
    grades = grade_many(trades)
    assert len(grades) == 4
    assert [g.trade_id for g in grades] == ["t-0", "t-1", "t-2", "t-3"]


def test_leak_distribution_counts_buckets() -> None:
    g = TradeGrader()
    bad_regime = g.grade(
        _trade(
            regime=TradeRegime.TRENDING_DOWN,
            side=TradeSide.LONG,
        )
    )
    bad_entry = g.grade(_trade(first_pullback=0.1))
    bad_both = g.grade(
        _trade(
            regime=TradeRegime.TRENDING_DOWN,
            side=TradeSide.LONG,
            first_pullback=0.1,
        )
    )
    dist = leak_distribution([bad_regime, bad_entry, bad_both])
    assert dist["regime_fit"] == 2
    assert dist["entry_timing"] == 2


# --------------------------------------------------------------------------- #
# GradeBreakdown model
# --------------------------------------------------------------------------- #


def test_grade_breakdown_total_rounds() -> None:
    b = GradeBreakdown(
        entry_timing=19.97,
        regime_fit=20.0,
        confluence_accuracy=20.0,
        exit_efficiency=20.0,
        rule_adherence=20.0,
    )
    assert b.total == 99.97


def test_grade_breakdown_rejects_over_max() -> None:
    with pytest.raises(ValidationError):
        GradeBreakdown(
            entry_timing=25.0,
            regime_fit=20.0,
            confluence_accuracy=20.0,
            exit_efficiency=20.0,
            rule_adherence=20.0,
        )
