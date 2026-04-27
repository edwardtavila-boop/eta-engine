"""Tests for backtest.exit_quality -- MAE/MFE analyzer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from eta_engine.backtest.exit_quality import (
    ExitQualityHeatmap,
    ExitQualityRow,
    MaeMfePoint,
    Side,
    analyze_batch,
    analyze_trade,
    build_heatmap,
    money_left_on_table,
    rank_setups_by_leak,
)

_T0 = datetime(2025, 1, 1, 9, 30, tzinfo=UTC)


def _point(
    *,
    trade_id: str = "t-1",
    setup: str = "breakout_retest",
    regime: str = "TRENDING_UP",
    side: Side = Side.LONG,
    entry: float = 100.0,
    exit_: float = 110.0,
    stop: float = 95.0,
    mfe: float = 110.0,
    mae: float = 99.0,
    hold_min: int = 30,
    time_to_mfe_min: int = 25,
) -> MaeMfePoint:
    return MaeMfePoint(
        trade_id=trade_id,
        symbol="MNQ",
        side=side,
        regime=regime,
        setup=setup,
        opened_at=_T0,
        closed_at=_T0 + timedelta(minutes=hold_min),
        entry_price=entry,
        exit_price=exit_,
        stop_price=stop,
        mfe_price=mfe,
        mae_price=mae,
        time_to_mfe_sec=time_to_mfe_min * 60.0,
    )


# --------------------------------------------------------------------------- #
# MaeMfePoint validation
# --------------------------------------------------------------------------- #


def test_point_rejects_reversed_times() -> None:
    with pytest.raises(ValidationError):
        MaeMfePoint(
            trade_id="x",
            symbol="MNQ",
            side=Side.LONG,
            regime="TRENDING_UP",
            setup="s",
            opened_at=_T0 + timedelta(minutes=1),
            closed_at=_T0,
            entry_price=1.0,
            exit_price=1.0,
            stop_price=0.5,
            mfe_price=1.1,
            mae_price=0.9,
            time_to_mfe_sec=30.0,
        )


def test_point_rejects_negative_time_to_mfe() -> None:
    with pytest.raises(ValidationError):
        MaeMfePoint(
            trade_id="x",
            symbol="MNQ",
            side=Side.LONG,
            regime="TRENDING_UP",
            setup="s",
            opened_at=_T0,
            closed_at=_T0 + timedelta(minutes=1),
            entry_price=1.0,
            exit_price=1.0,
            stop_price=0.5,
            mfe_price=1.1,
            mae_price=0.9,
            time_to_mfe_sec=-1.0,
        )


def test_point_rejects_empty_setup() -> None:
    with pytest.raises(ValidationError):
        _point(setup="")


# --------------------------------------------------------------------------- #
# R-metric properties
# --------------------------------------------------------------------------- #


def test_r_risk_long() -> None:
    p = _point(entry=100.0, stop=95.0)
    assert p.r_risk == 5.0


def test_r_captured_long_winner() -> None:
    p = _point(entry=100.0, exit_=110.0, stop=95.0, mfe=110.0)
    assert p.r_captured == 2.0


def test_r_captured_short_winner() -> None:
    p = _point(side=Side.SHORT, entry=100.0, exit_=90.0, stop=105.0, mfe=89.0, mae=101.0)
    assert p.r_captured == 2.0


def test_r_available_long() -> None:
    p = _point(entry=100.0, exit_=105.0, stop=95.0, mfe=115.0)
    assert p.r_available == 3.0


def test_r_adverse_long() -> None:
    p = _point(entry=100.0, stop=95.0, mae=97.0)
    assert p.r_adverse == pytest.approx(0.6, abs=1e-6)


def test_r_adverse_zero_if_mae_above_entry_long() -> None:
    p = _point(entry=100.0, stop=95.0, mae=101.0)
    assert p.r_adverse == 0.0


# --------------------------------------------------------------------------- #
# analyze_trade
# --------------------------------------------------------------------------- #


def test_analyze_perfect_exit_has_capture_1() -> None:
    p = _point(entry=100.0, exit_=110.0, stop=95.0, mfe=110.0)
    row = analyze_trade(p)
    assert row.capture_ratio == 1.0
    assert row.leaked_r == 0.0
    assert row.r_captured == 2.0
    assert row.r_available == 2.0


def test_analyze_half_exit_has_capture_half() -> None:
    p = _point(entry=100.0, exit_=105.0, stop=95.0, mfe=115.0)
    row = analyze_trade(p)
    # captured = 1, available = 3, ratio = 0.333
    assert row.capture_ratio == pytest.approx(0.333, abs=0.001)
    assert row.leaked_r == pytest.approx(2.0, abs=0.001)


def test_analyze_stop_out_has_capture_zero() -> None:
    p = _point(entry=100.0, exit_=95.0, stop=95.0, mfe=101.0)
    row = analyze_trade(p)
    assert row.capture_ratio == 0.0
    assert row.r_captured == -1.0


def test_analyze_stop_out_no_mfe_full_capture() -> None:
    # mae below entry, mfe AT entry -> r_available = 0; edge case
    p = _point(entry=100.0, exit_=95.0, stop=95.0, mfe=100.0)
    row = analyze_trade(p)
    assert row.r_available == 0.0
    # When no MFE was available, capture is degenerate; we treat as 0 for losses
    assert row.capture_ratio == 0.0


def test_analyze_hold_frac_and_score() -> None:
    # Hold 30 min, MFE at 25 min -> frac = 0.833
    p = _point(hold_min=30, time_to_mfe_min=25, entry=100.0, exit_=108.0, stop=95.0, mfe=110.0)
    row = analyze_trade(p)
    assert row.hold_frac_to_mfe == pytest.approx(0.833, abs=0.001)
    # capture_ratio = 8/10/ = 0.8
    # hold_score = 1 - (1-0.8) * (1-0.833) = 1 - 0.2*0.167 = 0.967
    assert row.hold_score == pytest.approx(0.967, abs=0.01)


def test_analyze_carries_regime_and_setup() -> None:
    p = _point(regime="CRISIS", setup="fade_open")
    row = analyze_trade(p)
    assert row.regime == "CRISIS"
    assert row.setup == "fade_open"


# --------------------------------------------------------------------------- #
# analyze_batch
# --------------------------------------------------------------------------- #


def test_analyze_batch_preserves_order() -> None:
    pts = [_point(trade_id=f"t-{i}") for i in range(4)]
    rows = analyze_batch(pts)
    assert [r.trade_id for r in rows] == ["t-0", "t-1", "t-2", "t-3"]


def test_analyze_batch_empty() -> None:
    assert analyze_batch([]) == []


# --------------------------------------------------------------------------- #
# build_heatmap
# --------------------------------------------------------------------------- #


def test_heatmap_groups_by_regime_and_setup() -> None:
    rows = [analyze_trade(_point(trade_id=f"t-{i}", regime="TRENDING_UP", setup="breakout")) for i in range(3)]
    rows += [analyze_trade(_point(trade_id=f"t-{i}", regime="TRENDING_UP", setup="fade")) for i in range(3, 5)]
    rows += [analyze_trade(_point(trade_id=f"t-{i}", regime="CRISIS", setup="fade")) for i in range(5, 6)]
    hm = build_heatmap(rows)
    assert len(hm) == 3
    assert hm[("TRENDING_UP", "breakout")].n == 3
    assert hm[("TRENDING_UP", "fade")].n == 2
    assert hm[("CRISIS", "fade")].n == 1


def test_heatmap_mean_capture_ratio() -> None:
    rows = [
        analyze_trade(
            _point(
                trade_id="perfect",
                entry=100.0,
                exit_=110.0,
                stop=95.0,
                mfe=110.0,  # 1.0
            )
        ),
        analyze_trade(
            _point(
                trade_id="half",
                entry=100.0,
                exit_=105.0,
                stop=95.0,
                mfe=115.0,  # 0.333
            )
        ),
    ]
    hm = build_heatmap(rows)[(rows[0].regime, rows[0].setup)]
    assert hm.mean_capture_ratio == pytest.approx((1.0 + 0.333) / 2, abs=0.01)
    assert hm.best_capture_ratio == 1.0


def test_heatmap_worst_leak() -> None:
    rows = [
        analyze_trade(_point(trade_id="t1", entry=100.0, exit_=105.0, stop=95.0, mfe=115.0)),  # 2R leaked
        analyze_trade(_point(trade_id="t2", entry=100.0, exit_=110.0, stop=95.0, mfe=110.0)),  # 0R leaked
    ]
    hm = list(build_heatmap(rows).values())[0]
    assert hm.worst_leak_r == pytest.approx(2.0, abs=0.001)


def test_heatmap_empty_input() -> None:
    assert build_heatmap([]) == {}


# --------------------------------------------------------------------------- #
# money_left_on_table
# --------------------------------------------------------------------------- #


def test_money_left_on_table_only_positive_leaks() -> None:
    rows = [
        analyze_trade(_point(trade_id="leaker", entry=100.0, exit_=105.0, stop=95.0, mfe=115.0)),  # 2R leak
        analyze_trade(_point(trade_id="perfect", entry=100.0, exit_=110.0, stop=95.0, mfe=110.0)),  # 0R leak
    ]
    total = money_left_on_table(rows, dollars_per_r=100.0)
    # Only the leaker contributes: 2R * $100 = $200
    assert total == pytest.approx(200.0, abs=0.01)


def test_money_left_on_table_rejects_nonpositive_dollars_per_r_rate() -> None:
    with pytest.raises(ValueError):
        money_left_on_table([], dollars_per_r=0.0)


def test_money_left_on_table_empty_batch() -> None:
    assert money_left_on_table([], dollars_per_r=100.0) == 0.0


# --------------------------------------------------------------------------- #
# rank_setups_by_leak
# --------------------------------------------------------------------------- #


def test_rank_setups_by_leak_descending() -> None:
    rows = [
        analyze_trade(_point(trade_id="a", setup="fade", entry=100.0, exit_=105.0, stop=95.0, mfe=120.0)),  # 3R leak
        analyze_trade(
            _point(trade_id="b", setup="breakout", entry=100.0, exit_=108.0, stop=95.0, mfe=110.0)
        ),  # 0.4R leak
        analyze_trade(_point(trade_id="c", setup="fade", entry=100.0, exit_=102.0, stop=95.0, mfe=112.0)),  # 2R leak
    ]
    ranked = rank_setups_by_leak(rows)
    assert ranked[0][0] == "fade"
    assert ranked[0][1] == pytest.approx(5.0, abs=0.001)
    assert ranked[1][0] == "breakout"


def test_rank_setups_empty_when_no_leaks() -> None:
    rows = [
        analyze_trade(_point(trade_id="x", entry=100.0, exit_=110.0, stop=95.0, mfe=110.0)),
    ]
    assert rank_setups_by_leak(rows) == []


# --------------------------------------------------------------------------- #
# ExitQualityRow model sanity
# --------------------------------------------------------------------------- #


def test_row_rejects_capture_ratio_over_1() -> None:
    with pytest.raises(ValidationError):
        ExitQualityRow(
            trade_id="x",
            regime="R",
            setup="s",
            r_captured=1.0,
            r_available=1.0,
            r_adverse=0.0,
            capture_ratio=1.5,
            hold_frac_to_mfe=0.5,
            hold_score=0.5,
            leaked_r=0.0,
        )


def test_heatmap_rejects_negative_n() -> None:
    with pytest.raises(ValidationError):
        ExitQualityHeatmap(
            regime="R",
            setup="s",
            n=-1,
            mean_capture_ratio=0.5,
            mean_r_captured=0.0,
            mean_r_available=0.0,
            mean_leaked_r=0.0,
            worst_leak_r=0.0,
            best_capture_ratio=0.0,
        )
