"""Tests for eta_engine.brain.synthetic."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.brain.regime import RegimeType
from eta_engine.brain.synthetic import (
    PROFILES,
    Bar,
    RegimeProfile,
    SyntheticBarGenerator,
    fit_profile_from_bars,
    get_profile,
)

_T0 = datetime(2025, 1, 1, tzinfo=UTC)
_START_PRICE = 17_500.0


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


def test_profiles_cover_every_regime() -> None:
    for r in RegimeType:
        # get_profile should not raise for any enum member
        assert get_profile(r) is PROFILES[r]


def test_crisis_profile_is_more_extreme_than_low_vol() -> None:
    crisis = get_profile(RegimeType.CRISIS)
    low = get_profile(RegimeType.LOW_VOL)
    assert crisis.sigma > low.sigma * 5.0
    assert crisis.tail_weight > low.tail_weight
    assert crisis.vol_persistence > low.vol_persistence
    assert crisis.intrabar_range_mult >= low.intrabar_range_mult


def test_profile_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        RegimeProfile(mu=0.0, sigma=0.0)  # sigma must be > 0
    with pytest.raises(ValueError):
        RegimeProfile(mu=0.0, sigma=0.01, vol_persistence=1.0)  # lt=1
    with pytest.raises(ValueError):
        RegimeProfile(mu=0.0, sigma=0.01, tail_df=2.0)  # gt=2
    with pytest.raises(ValueError):
        RegimeProfile(mu=0.0, sigma=0.01, intrabar_range_mult=0.0)  # gt=0


# --------------------------------------------------------------------------- #
# Bar invariants
# --------------------------------------------------------------------------- #


def test_bar_rejects_nonpositive_prices() -> None:
    with pytest.raises(ValueError):
        Bar(ts=_T0, open=0.0, high=1.0, low=1.0, close=1.0, volume=0.0)
    with pytest.raises(ValueError):
        Bar(ts=_T0, open=1.0, high=-1.0, low=1.0, close=1.0, volume=0.0)


def test_bar_accepts_zero_volume() -> None:
    b = Bar(ts=_T0, open=1.0, high=1.0, low=1.0, close=1.0, volume=0.0)
    assert b.volume == 0.0


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_same_seed_produces_identical_series() -> None:
    a = SyntheticBarGenerator(regime=RegimeType.TRENDING, seed=42)
    b = SyntheticBarGenerator(regime=RegimeType.TRENDING, seed=42)
    xs = a.generate_series(n=20, start_price=_START_PRICE, start_ts=_T0)
    ys = b.generate_series(n=20, start_price=_START_PRICE, start_ts=_T0)
    for x, y in zip(xs, ys, strict=True):
        assert x.close == y.close
        assert x.high == y.high
        assert x.low == y.low
        assert x.volume == y.volume


def test_different_seed_produces_different_series() -> None:
    a = SyntheticBarGenerator(regime=RegimeType.TRENDING, seed=1)
    b = SyntheticBarGenerator(regime=RegimeType.TRENDING, seed=2)
    xs = a.generate_series(n=20, start_price=_START_PRICE, start_ts=_T0)
    ys = b.generate_series(n=20, start_price=_START_PRICE, start_ts=_T0)
    # At least one close must differ between runs
    assert any(x.close != y.close for x, y in zip(xs, ys, strict=True))


# --------------------------------------------------------------------------- #
# OHLCV structural invariants
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("regime", list(RegimeType))
def test_generated_bars_respect_ohlc_invariants(regime: RegimeType) -> None:
    gen = SyntheticBarGenerator(regime=regime, seed=11)
    bars = gen.generate_series(n=200, start_price=_START_PRICE, start_ts=_T0)
    assert len(bars) == 200
    for b in bars:
        assert b.high >= max(b.open, b.close) - 1e-9
        assert b.low <= min(b.open, b.close) + 1e-9
        assert b.low > 0.0
        assert b.volume >= 0.0
        assert b.synthetic is True


def test_series_is_chained_open_matches_prev_close() -> None:
    gen = SyntheticBarGenerator(regime=RegimeType.RANGING, seed=7)
    bars = gen.generate_series(n=50, start_price=_START_PRICE, start_ts=_T0)
    for prev, curr in zip(bars[:-1], bars[1:], strict=True):
        assert curr.open == prev.close


def test_timestamps_advance_by_step() -> None:
    gen = SyntheticBarGenerator(regime=RegimeType.RANGING, seed=7)
    bars = gen.generate_series(
        n=10,
        start_price=_START_PRICE,
        start_ts=_T0,
        step_seconds=60,
    )
    for prev, curr in zip(bars[:-1], bars[1:], strict=True):
        assert curr.ts - prev.ts == timedelta(seconds=60)


def test_first_bar_open_equals_start_price() -> None:
    gen = SyntheticBarGenerator(regime=RegimeType.LOW_VOL, seed=3)
    bars = gen.generate_series(n=5, start_price=12_345.0, start_ts=_T0)
    assert bars[0].open == 12_345.0


# --------------------------------------------------------------------------- #
# Regime sensitivity
# --------------------------------------------------------------------------- #


def _series_sigma(bars: list[Bar]) -> float:
    rets = [math.log(bars[i].close / bars[i - 1].close) for i in range(1, len(bars))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
    return math.sqrt(var)


def test_crisis_series_has_higher_realized_vol_than_low_vol() -> None:
    # Long runs so sample estimates are stable.
    low = SyntheticBarGenerator(regime=RegimeType.LOW_VOL, seed=5).generate_series(
        n=1000,
        start_price=_START_PRICE,
        start_ts=_T0,
    )
    crisis = SyntheticBarGenerator(regime=RegimeType.CRISIS, seed=5).generate_series(
        n=1000,
        start_price=_START_PRICE,
        start_ts=_T0,
    )
    assert _series_sigma(crisis) > _series_sigma(low) * 5.0


def test_high_vol_series_has_higher_realized_vol_than_ranging() -> None:
    ranging = SyntheticBarGenerator(regime=RegimeType.RANGING, seed=2).generate_series(
        n=1000,
        start_price=_START_PRICE,
        start_ts=_T0,
    )
    hv = SyntheticBarGenerator(regime=RegimeType.HIGH_VOL, seed=2).generate_series(
        n=1000,
        start_price=_START_PRICE,
        start_ts=_T0,
    )
    assert _series_sigma(hv) > _series_sigma(ranging) * 2.0


# --------------------------------------------------------------------------- #
# Control
# --------------------------------------------------------------------------- #


def test_set_regime_changes_profile() -> None:
    gen = SyntheticBarGenerator(regime=RegimeType.LOW_VOL, seed=9)
    assert gen.regime == RegimeType.LOW_VOL
    assert gen.profile.sigma == PROFILES[RegimeType.LOW_VOL].sigma
    gen.set_regime(RegimeType.CRISIS)
    assert gen.regime == RegimeType.CRISIS
    assert gen.profile.sigma == PROFILES[RegimeType.CRISIS].sigma


def test_set_regime_with_custom_profile_overrides_default() -> None:
    custom = RegimeProfile(mu=0.0, sigma=0.5, vol_persistence=0.9)
    gen = SyntheticBarGenerator(regime=RegimeType.RANGING, seed=1)
    gen.set_regime(RegimeType.CRISIS, profile=custom)
    assert gen.profile is custom


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


def test_next_bar_rejects_nonpositive_prev_close() -> None:
    gen = SyntheticBarGenerator(regime=RegimeType.RANGING, seed=1)
    with pytest.raises(ValueError, match="prev_close"):
        gen.next_bar(prev_close=0.0, ts=_T0)
    with pytest.raises(ValueError, match="prev_close"):
        gen.next_bar(prev_close=-1.0, ts=_T0)


def test_generate_series_rejects_bad_args() -> None:
    gen = SyntheticBarGenerator(regime=RegimeType.RANGING, seed=1)
    with pytest.raises(ValueError, match="n"):
        gen.generate_series(n=0, start_price=_START_PRICE, start_ts=_T0)
    with pytest.raises(ValueError, match="start_price"):
        gen.generate_series(n=1, start_price=0.0, start_ts=_T0)


# --------------------------------------------------------------------------- #
# Augmentation
# --------------------------------------------------------------------------- #


def _real_bars(n: int) -> list[Bar]:
    """Fabricate a sequence of 'real' bars for augment() tests."""
    out: list[Bar] = []
    t = _T0
    price = _START_PRICE
    for i in range(n):
        out.append(
            Bar(
                ts=t,
                open=price,
                high=price + 5,
                low=price - 5,
                close=price + (2 if i % 2 else -2),
                volume=500.0,
                synthetic=False,
            )
        )
        t += timedelta(minutes=5)
        price = out[-1].close
    return out


def test_augment_length_is_real_plus_synth_per_real() -> None:
    gen = SyntheticBarGenerator(regime=RegimeType.TRENDING, seed=1)
    real = _real_bars(10)
    out = gen.augment(real, n_synth_per_real=3)
    assert len(out) == 10 * (1 + 3)


def test_augment_preserves_real_bars_with_flag_off() -> None:
    gen = SyntheticBarGenerator(regime=RegimeType.TRENDING, seed=1)
    # Deliberately force real bars into 'synthetic=True' to test the flag reset
    real = [b.model_copy(update={"synthetic": True}) for b in _real_bars(4)]
    out = gen.augment(real, n_synth_per_real=0)
    assert len(out) == 4
    for r, o in zip(real, out, strict=True):
        assert o.close == r.close
        assert o.synthetic is False


def test_augment_timestamps_interleave_correctly() -> None:
    gen = SyntheticBarGenerator(regime=RegimeType.TRENDING, seed=1)
    real = _real_bars(2)  # 2 real bars, 5min apart
    out = gen.augment(real, n_synth_per_real=2, step_seconds=60)
    # Expected: real0, synth at +60, synth at +120, real1, synth at real1+60, synth at real1+120
    assert out[0].ts == real[0].ts
    assert out[1].ts == real[0].ts + timedelta(seconds=60)
    assert out[2].ts == real[0].ts + timedelta(seconds=120)
    assert out[3].ts == real[1].ts
    assert out[4].ts == real[1].ts + timedelta(seconds=60)
    assert out[5].ts == real[1].ts + timedelta(seconds=120)


def test_augment_zero_synth_is_passthrough_but_resets_flag() -> None:
    gen = SyntheticBarGenerator(regime=RegimeType.TRENDING, seed=1)
    real = _real_bars(3)
    out = gen.augment(real, n_synth_per_real=0)
    assert len(out) == 3
    assert all(b.synthetic is False for b in out)


def test_augment_rejects_negative_synth_count() -> None:
    gen = SyntheticBarGenerator(regime=RegimeType.TRENDING, seed=1)
    with pytest.raises(ValueError, match="n_synth_per_real"):
        gen.augment(_real_bars(1), n_synth_per_real=-1)


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #


def test_fit_profile_recovers_sigma_within_tolerance() -> None:
    # Generate a long series with a known profile, then re-fit.
    true_sigma = 0.004
    true_profile = RegimeProfile(
        mu=0.0001,
        sigma=true_sigma,
        vol_persistence=0.0,
        tail_weight=0.0,
    )
    gen = SyntheticBarGenerator(
        regime=RegimeType.TRENDING,
        seed=42,
        profile=true_profile,
    )
    bars = gen.generate_series(n=2000, start_price=_START_PRICE, start_ts=_T0)
    fitted = fit_profile_from_bars(bars, regime=RegimeType.TRENDING)
    # Within 20% is very generous; iid Gaussian with n=2000 should be closer
    assert abs(fitted.sigma - true_sigma) / true_sigma < 0.20


def test_fit_profile_rejects_short_input() -> None:
    with pytest.raises(ValueError, match="bars"):
        fit_profile_from_bars([], regime=RegimeType.TRENDING)
    with pytest.raises(ValueError, match="bars"):
        fit_profile_from_bars(_real_bars(2), regime=RegimeType.TRENDING)


def test_fit_profile_keeps_default_tail_knobs() -> None:
    bars = SyntheticBarGenerator(
        regime=RegimeType.TRENDING,
        seed=3,
    ).generate_series(n=500, start_price=_START_PRICE, start_ts=_T0)
    fitted = fit_profile_from_bars(bars, regime=RegimeType.CRISIS)
    # Calibration does not estimate tail_weight / tail_df -> defaults preserved
    default = get_profile(RegimeType.CRISIS)
    assert fitted.tail_weight == default.tail_weight
    assert fitted.tail_df == default.tail_df


# --------------------------------------------------------------------------- #
# Roundtrip
# --------------------------------------------------------------------------- #


def test_bar_model_dump_roundtrip() -> None:
    gen = SyntheticBarGenerator(regime=RegimeType.RANGING, seed=1)
    bar = gen.next_bar(prev_close=_START_PRICE, ts=_T0)
    d = bar.model_dump()
    bar2 = Bar(**d)
    assert bar2.close == bar.close
    assert bar2.volume == bar.volume
    assert bar2.synthetic is True
