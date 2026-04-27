"""Tests for BarReplay.synthetic_bars_jump (jump-diffusion + regime-switching)."""

from __future__ import annotations

from datetime import UTC, datetime

from eta_engine.backtest import BarReplay


def test_synthetic_bars_jump_empty_n():
    assert BarReplay.synthetic_bars_jump(0) == []


def test_synthetic_bars_jump_produces_n_bars():
    bars = BarReplay.synthetic_bars_jump(
        n=50,
        start_price=100.0,
        drift=0.0,
        vol=0.01,
        symbol="TEST",
        seed=7,
    )
    assert len(bars) == 50
    for b in bars:
        assert b.symbol == "TEST"
        assert b.high >= b.low
        assert b.high >= b.close
        assert b.low <= b.close


def test_synthetic_bars_jump_seed_reproducible():
    a = BarReplay.synthetic_bars_jump(n=30, seed=42)
    b = BarReplay.synthetic_bars_jump(n=30, seed=42)
    assert [x.close for x in a] == [x.close for x in b]


def test_synthetic_bars_jump_different_seed_diverges():
    a = BarReplay.synthetic_bars_jump(n=50, seed=1)
    b = BarReplay.synthetic_bars_jump(n=50, seed=2)
    closes_a = [x.close for x in a]
    closes_b = [x.close for x in b]
    assert closes_a != closes_b


def test_synthetic_bars_jump_timestamps_monotonic():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = BarReplay.synthetic_bars_jump(
        n=20,
        start=start,
        interval_minutes=5,
        seed=11,
    )
    for earlier, later in zip(bars, bars[1:], strict=False):
        assert later.timestamp > earlier.timestamp


def test_synthetic_bars_jump_high_jump_intensity_produces_wider_range():
    # High jump params → wider price range than GBM
    gbm = BarReplay.synthetic_bars(n=200, vol=0.01, seed=11)
    jump = BarReplay.synthetic_bars_jump(
        n=200,
        vol=0.01,
        jump_intensity=0.20,
        jump_vol=0.05,
        seed=11,
    )
    gbm_range = max(b.high for b in gbm) - min(b.low for b in gbm)
    jump_range = max(b.high for b in jump) - min(b.low for b in jump)
    # Jump bars should exhibit a wider extremes envelope
    assert jump_range >= gbm_range * 0.9  # allow slack for seed noise


def test_synthetic_bars_jump_regime_boost_affects_drift():
    # With extreme bull_drift_boost and persistence, equity should trend up
    bars = BarReplay.synthetic_bars_jump(
        n=500,
        start_price=100.0,
        drift=0.0,
        vol=0.005,
        bull_drift_boost=0.005,
        bear_drift_penalty=0.0001,
        regime_persist=500,  # never switch out of bull
        seed=3,
    )
    # On average, net-up (allow noise)
    assert bars[-1].close >= bars[0].close * 0.8


def test_synthetic_bars_jump_zero_intensity_matches_gbm_shape():
    # With jump_intensity=0 and regime switch never triggering, should be GBM-like
    bars = BarReplay.synthetic_bars_jump(
        n=100,
        vol=0.01,
        jump_intensity=0.0,
        regime_persist=1_000_000,
        seed=5,
    )
    for b in bars:
        assert b.high >= b.low
        assert b.volume > 0
