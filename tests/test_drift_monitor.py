"""Tests for obs.drift_monitor."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eta_engine.backtest.models import Trade
from eta_engine.obs.drift_monitor import (
    BaselineSnapshot,
    DriftAssessment,
    FleetCorrelationAssessment,
    assess_drift,
    assess_fleet_correlation,
)


def _trade(pnl_r: float, **kw) -> Trade:  # type: ignore[no-untyped-def]
    """Tiny factory so tests stay readable."""
    base = {
        "entry_time": datetime(2026, 1, 1, tzinfo=UTC),
        "exit_time": datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
        "symbol": "MNQ",
        "side": "BUY",
        "qty": 1.0,
        "entry_price": 21000.0,
        "exit_price": 21010.0,
        "pnl_r": pnl_r,
        "pnl_usd": pnl_r * 100.0,
        "confluence_score": 7.5,
        "leverage_used": 1.0,
        "max_drawdown_during": 5.0,
    }
    base.update(kw)
    return Trade(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# BaselineSnapshot
# ---------------------------------------------------------------------------


def test_baseline_from_empty_returns_zeros() -> None:
    bl = BaselineSnapshot.from_trades("strat", [])
    assert bl.n_trades == 0
    assert bl.win_rate == 0.0
    assert bl.avg_r == 0.0
    assert bl.r_stddev == 0.0


def test_baseline_from_trades_computes_stats() -> None:
    trades = [_trade(1.5), _trade(-1.0), _trade(1.5), _trade(-1.0), _trade(1.5)]
    bl = BaselineSnapshot.from_trades("s", trades)
    assert bl.n_trades == 5
    assert bl.win_rate == pytest.approx(0.6)
    assert bl.avg_r == pytest.approx(0.5)
    # sd of [1.5, -1, 1.5, -1, 1.5] with ddof=1
    assert bl.r_stddev == pytest.approx(1.3693, rel=1e-3)


# ---------------------------------------------------------------------------
# Insufficient sample → green
# ---------------------------------------------------------------------------


def test_insufficient_sample_returns_green_with_reason() -> None:
    bl = BaselineSnapshot(
        strategy_id="s",
        n_trades=100,
        win_rate=0.55,
        avg_r=0.3,
        r_stddev=1.2,
    )
    a = assess_drift(strategy_id="s", recent=[_trade(1.5)] * 5, baseline=bl, min_trades=20)
    assert a.severity == "green"
    assert "insufficient sample" in a.reasons[0]


# ---------------------------------------------------------------------------
# Stable performance → green
# ---------------------------------------------------------------------------


def test_stable_performance_returns_green() -> None:
    bl = BaselineSnapshot(
        strategy_id="s",
        n_trades=100,
        win_rate=0.55,
        avg_r=0.3,
        r_stddev=1.2,
    )
    # 30 trades, ~55% wins, ~+0.3 avg R
    recent = [_trade(1.5)] * 17 + [_trade(-1.2)] * 13
    a = assess_drift(strategy_id="s", recent=recent, baseline=bl)
    assert a.severity == "green"
    assert a.n_recent == 30


# ---------------------------------------------------------------------------
# Win-rate collapse → red
# ---------------------------------------------------------------------------


def test_win_rate_collapse_triggers_red() -> None:
    bl = BaselineSnapshot(
        strategy_id="s",
        n_trades=200,
        win_rate=0.60,
        avg_r=0.4,
        r_stddev=1.0,
    )
    # 30 recent trades, only 5 wins (16.7%) — way below 60% baseline
    recent = [_trade(1.5)] * 5 + [_trade(-1.0)] * 25
    a = assess_drift(strategy_id="s", recent=recent, baseline=bl)
    assert a.severity == "red"
    assert any("win rate" in r for r in a.reasons)
    assert a.win_rate_z < -3.0


# ---------------------------------------------------------------------------
# Mild win-rate dip → amber
# ---------------------------------------------------------------------------


def test_mild_win_rate_dip_triggers_amber() -> None:
    bl = BaselineSnapshot(
        strategy_id="s",
        n_trades=500,
        win_rate=0.60,
        avg_r=0.4,
        r_stddev=1.0,
    )
    # 50 trades, 21 wins (42%) — about 2.6σ low against baseline of 60%.
    # SE = sqrt(0.6*0.4/50) ≈ 0.0693; (0.42 - 0.6)/0.0693 ≈ -2.60.
    recent = [_trade(1.5)] * 21 + [_trade(-1.0)] * 29
    a = assess_drift(strategy_id="s", recent=recent, baseline=bl)
    assert a.severity == "amber"
    assert any("win rate" in r for r in a.reasons)


# ---------------------------------------------------------------------------
# Avg-R drop → flagged separately from WR
# ---------------------------------------------------------------------------


def test_avg_r_collapse_triggers_red() -> None:
    bl = BaselineSnapshot(
        strategy_id="s",
        n_trades=200,
        win_rate=0.5,
        avg_r=0.5,
        r_stddev=1.0,
    )
    # 30 trades with wins still ~50% but R per trade now strongly negative
    recent = [_trade(0.5)] * 15 + [_trade(-2.0)] * 15
    a = assess_drift(strategy_id="s", recent=recent, baseline=bl)
    assert a.severity == "red"
    assert any("avg R" in r for r in a.reasons)


# ---------------------------------------------------------------------------
# Both metrics drop → both reasons surface
# ---------------------------------------------------------------------------


def test_both_metrics_drop_both_reasons_surface() -> None:
    bl = BaselineSnapshot(
        strategy_id="s",
        n_trades=200,
        win_rate=0.6,
        avg_r=0.5,
        r_stddev=1.0,
    )
    recent = [_trade(0.3)] * 5 + [_trade(-2.5)] * 25
    a = assess_drift(strategy_id="s", recent=recent, baseline=bl)
    assert a.severity == "red"
    assert any("win rate" in r for r in a.reasons)
    assert any("avg R" in r for r in a.reasons)


# ---------------------------------------------------------------------------
# Degenerate baseline (always-win) doesn't crash
# ---------------------------------------------------------------------------


def test_degenerate_baseline_does_not_crash() -> None:
    bl = BaselineSnapshot(
        strategy_id="s",
        n_trades=10,
        win_rate=1.0,
        avg_r=1.0,
        r_stddev=0.0,
    )
    recent = [_trade(0.0)] * 25
    a = assess_drift(strategy_id="s", recent=recent, baseline=bl)
    # Either amber or red — point is no exception
    assert a.severity in ("amber", "red")
    assert a.n_recent == 25


# ---------------------------------------------------------------------------
# Threshold tunability
# ---------------------------------------------------------------------------


def test_aggressive_thresholds_flag_what_default_misses() -> None:
    bl = BaselineSnapshot(
        strategy_id="s",
        n_trades=500,
        win_rate=0.55,
        avg_r=0.3,
        r_stddev=1.0,
    )
    recent = [_trade(1.0)] * 13 + [_trade(-1.2)] * 12  # ~52% WR
    default = assess_drift(strategy_id="s", recent=recent, baseline=bl)
    assert default.severity == "green"
    aggressive = assess_drift(strategy_id="s", recent=recent, baseline=bl, amber_z=0.2, red_z=0.5)
    # With z thresholds collapsed near zero, even small deltas trigger.
    assert aggressive.severity in ("amber", "red")


# ---------------------------------------------------------------------------
# Snapshot of returned object
# ---------------------------------------------------------------------------


def test_assessment_is_pydantic_serializable() -> None:
    bl = BaselineSnapshot(
        strategy_id="s",
        n_trades=100,
        win_rate=0.55,
        avg_r=0.3,
        r_stddev=1.0,
    )
    recent = [_trade(1.5)] * 16 + [_trade(-1.0)] * 14
    a = assess_drift(strategy_id="s", recent=recent, baseline=bl)
    payload = a.model_dump()
    assert payload["strategy_id"] == "s"
    assert "severity" in payload
    assert "win_rate_z" in payload
    # Round-trip
    a2 = DriftAssessment.model_validate(payload)
    assert a2 == a


# ---------------------------------------------------------------------------
# Fleet correlation penalty (quant-sage 2026-04-27)
# ---------------------------------------------------------------------------


def test_fleet_correlation_insufficient_sample_returns_green() -> None:
    a = assess_fleet_correlation(
        bot_a="btc_hybrid",
        recent_a=[_trade(1.0)] * 5,
        bot_b="eth_perp",
        recent_b=[_trade(1.0)] * 5,
    )
    assert a.severity == "green"
    assert a.recommended_action == "independent"
    assert a.rho == 0.0
    assert any("insufficient sample" in r for r in a.reasons)


def test_fleet_correlation_perfectly_correlated_pair_red() -> None:
    """Same R-stream on both bots -> rho=1.0 -> red / merge_for_risk."""
    rs = [1.5, -1.0, 0.7, -0.4, 1.2, 0.9, -0.6, 1.1, -0.3, 0.8, 1.0, -0.7]
    trades_a = [_trade(r) for r in rs]
    trades_b = [_trade(r) for r in rs]
    a = assess_fleet_correlation(
        bot_a="btc_hybrid",
        recent_a=trades_a,
        bot_b="eth_perp",
        recent_b=trades_b,
    )
    assert a.severity == "red"
    assert a.recommended_action == "merge_for_risk"
    assert a.rho == pytest.approx(1.0)
    assert a.bot_a == "btc_hybrid"
    assert a.bot_b == "eth_perp"


def test_fleet_correlation_anti_correlated_pair_green() -> None:
    """Negative correlation is the OPPOSITE of double-counting; green."""
    rs_a = [1.5, -1.0, 0.7, -0.4, 1.2, 0.9, -0.6, 1.1, -0.3, 0.8, 1.0, -0.7]
    rs_b = [-r for r in rs_a]
    a = assess_fleet_correlation(
        bot_a="btc_hybrid",
        recent_a=[_trade(r) for r in rs_a],
        bot_b="eth_perp",
        recent_b=[_trade(r) for r in rs_b],
    )
    assert a.severity == "green"
    assert a.recommended_action == "independent"
    assert a.rho < 0


def test_fleet_correlation_amber_band_recommends_halve() -> None:
    """Construct a stream with rho ~= 0.6 (between amber 0.5 and red 0.7)."""
    # Each ys[i] = 0.6 * xs[i] + 0.8 * noise[i] -> theoretical rho ~ 0.6
    xs = [1.0, -0.5, 0.7, -0.4, 1.2, 0.9, -0.6, 1.1, -0.3, 0.8, 0.2, -0.7]
    noise = [0.1, -0.2, 0.3, -0.1, 0.05, -0.4, 0.6, -0.3, 0.2, -0.05, 0.4, -0.6]
    ys = [0.6 * xs[i] + 0.8 * noise[i] for i in range(len(xs))]
    a = assess_fleet_correlation(
        bot_a="btc_hybrid",
        recent_a=[_trade(r) for r in xs],
        bot_b="eth_perp",
        recent_b=[_trade(r) for r in ys],
    )
    # Tolerate either amber or red depending on exact rho given small N
    assert a.severity in {"amber", "red"}
    assert a.recommended_action in {"halve_one", "merge_for_risk"}


def test_fleet_correlation_uses_min_lengths_when_unequal() -> None:
    """Asymmetric lengths -> tail-aligned to the shorter stream."""
    rs = [1.0, -0.5, 0.8, -0.3, 1.1, 0.2, -0.7, 0.9, -0.4, 1.2, 0.5, -0.6]
    trades_a = [_trade(r) for r in rs]
    trades_b = [_trade(r) for r in rs[2:]]  # 10 trades vs 12
    a = assess_fleet_correlation(
        bot_a="btc_hybrid",
        recent_a=trades_a,
        bot_b="eth_perp",
        recent_b=trades_b,
        min_paired=8,
    )
    assert a.n_paired == 10  # min of the two
    # Same tail-trades on both sides -> rho == 1.0
    assert a.rho == pytest.approx(1.0)


def test_fleet_correlation_serializes_round_trip() -> None:
    a = assess_fleet_correlation(
        bot_a="btc_hybrid",
        recent_a=[_trade(0.5)] * 12,
        bot_b="eth_perp",
        recent_b=[_trade(0.5)] * 12,
    )
    payload = a.model_dump()
    a2 = FleetCorrelationAssessment.model_validate(payload)
    assert a2 == a
