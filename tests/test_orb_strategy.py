"""Tests for strategies.orb_strategy — Opening Range Breakout for MNQ/NQ."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.orb_strategy import ORBConfig, ORBStrategy, _add_minutes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_NY = ZoneInfo("America/New_York")


def _bar(
    local_h: int,
    local_m: int,
    *,
    high: float,
    low: float,
    open_: float | None = None,
    close: float | None = None,
    volume: float = 1000.0,
    day: int = 15,
) -> BarData:
    """Construct a 5m bar at New York local time on 2026-01-DD."""
    local_dt = datetime(2026, 1, day, local_h, local_m, tzinfo=_NY)
    utc_dt = local_dt.astimezone(UTC)
    o = open_ if open_ is not None else (high + low) / 2
    c = close if close is not None else (high + low) / 2
    return BarData(
        timestamp=utc_dt,
        symbol="MNQ",
        open=o,
        high=high,
        low=low,
        close=c,
        volume=volume,
    )


def _config_for_test() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 1, 31, tzinfo=UTC),
        symbol="MNQ",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=5.0,
        max_trades_per_day=10,
    )


def _fixture_strategy(**overrides) -> ORBStrategy:  # type: ignore[no-untyped-def]
    """ORB with EMA bias disabled by default so tests don't need a long warmup.

    NOTE: ``require_retest=False`` is set explicitly so existing tests
    exercise the immediate-breakout entry path. The strategy default
    flipped to ``require_retest=True`` (a quality improvement that
    reduces false breakouts) but the breakout-mechanic tests below
    predate that change. Tests that specifically exercise the retest
    flow override ``require_retest=True`` themselves.
    """
    base = {
        "ema_bias_period": 0,
        "volume_mult": 0.0,
        "atr_period": 5,  # short so 5-bar warmup is enough
        "require_retest": False,
    }
    base.update(overrides)
    return ORBStrategy(ORBConfig(**base))


# ---------------------------------------------------------------------------
# _add_minutes
# ---------------------------------------------------------------------------


def test_add_minutes_no_wrap() -> None:
    assert _add_minutes(time(9, 30), 15) == time(9, 45)


def test_add_minutes_wraps_at_midnight() -> None:
    assert _add_minutes(time(23, 30), 60) == time(0, 30)


# ---------------------------------------------------------------------------
# Range building
# ---------------------------------------------------------------------------


def test_no_entry_during_range_window() -> None:
    s = _fixture_strategy(range_minutes=15)
    cfg = _config_for_test()
    # Range bars: 9:30, 9:35, 9:40
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        bar = _bar(h, m, high=100.0 + m, low=100.0)
        assert s.maybe_enter(bar, [], 10_000.0, cfg) is None


def test_range_completes_after_window() -> None:
    s = _fixture_strategy(range_minutes=15)
    cfg = _config_for_test()
    # Build range
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=100.0 + m, low=100.0), [], 10_000.0, cfg)
    assert s._day is not None
    assert s._day.range_complete is False
    # First post-range bar
    s.maybe_enter(_bar(9, 45, high=100.5, low=100.0), [], 10_000.0, cfg)
    assert s._day.range_complete is True


# ---------------------------------------------------------------------------
# Breakout detection
# ---------------------------------------------------------------------------


def test_long_breakout_above_range_high() -> None:
    s = _fixture_strategy(range_minutes=15)
    cfg = _config_for_test()
    # Build range with high=120, low=100
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0), [], 10_000.0, cfg)
    # Need ATR history
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]
    # Breakout bar: high=125 > 120
    bar = _bar(9, 45, high=125.0, low=120.0, close=124.0)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert out.entry_price == 124.0


def test_short_breakout_below_range_low() -> None:
    s = _fixture_strategy(range_minutes=15)
    cfg = _config_for_test()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0), [], 10_000.0, cfg)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]
    bar = _bar(9, 45, high=100.0, low=95.0, close=96.0)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "SELL"


def test_retest_touch_without_close_confirmation_waits() -> None:
    s = _fixture_strategy(
        range_minutes=15,
        require_retest=True,
        retest_require_close_bounce=True,
    )
    cfg = _config_for_test()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0), [], 10_000.0, cfg)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]

    breakout = _bar(9, 45, high=125.0, low=121.0, close=124.0)
    assert s.maybe_enter(breakout, hist, 10_000.0, cfg) is None
    assert s._day.pending_breakout is True

    retest_touch = _bar(9, 50, high=121.0, low=119.0, close=119.5)
    assert s.maybe_enter(retest_touch, hist + [breakout], 10_000.0, cfg) is None
    assert s._day.retest_done is True
    assert s._day.pending_breakout is True

    confirmed = _bar(9, 55, high=123.0, low=119.5, close=121.0)
    out = s.maybe_enter(confirmed, hist + [breakout, retest_touch], 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"


def test_no_breakout_inside_range() -> None:
    s = _fixture_strategy(range_minutes=15)
    cfg = _config_for_test()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0), [], 10_000.0, cfg)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]
    # Bar fully inside range
    bar = _bar(9, 45, high=115.0, low=105.0, close=110.0)
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None


def test_only_one_trade_per_day() -> None:
    s = _fixture_strategy(range_minutes=15, max_trades_per_day=1)
    cfg = _config_for_test()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0), [], 10_000.0, cfg)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]
    out1 = s.maybe_enter(_bar(9, 45, high=125.0, low=120.0, close=124.0), hist, 10_000.0, cfg)
    out2 = s.maybe_enter(_bar(9, 50, high=126.0, low=124.0, close=125.5), hist, 10_000.0, cfg)
    assert out1 is not None
    assert out2 is None


def test_state_resets_next_day() -> None:
    s = _fixture_strategy(range_minutes=15)
    cfg = _config_for_test()
    # Day 1 — fire
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0, day=15), [], 10_000.0, cfg)
    hist = [_bar(8, m, high=120.0, low=100.0, day=15) for m in range(0, 30, 5)]
    s.maybe_enter(_bar(9, 45, high=125.0, low=120.0, close=124.0, day=15), hist, 10_000.0, cfg)
    # Day 2 — fresh range
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        out = s.maybe_enter(_bar(h, m, high=130.0, low=110.0, day=16), hist, 10_000.0, cfg)
        assert out is None  # still in range window


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_no_entry_after_max_entry_local() -> None:
    s = _fixture_strategy(range_minutes=15, max_entry_local=time(11, 0))
    cfg = _config_for_test()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0), [], 10_000.0, cfg)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]
    # 11:00 is exactly the cutoff — should be blocked
    bar = _bar(11, 0, high=125.0, low=120.0, close=124.0)
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None
    bar = _bar(11, 30, high=125.0, low=120.0, close=124.0)
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None


def test_min_range_filter_blocks_narrow_open() -> None:
    s = _fixture_strategy(range_minutes=15, min_range_pts=10.0)
    cfg = _config_for_test()
    # Narrow range: 5 points
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=105.0, low=100.0), [], 10_000.0, cfg)
    hist = [_bar(8, m, high=105.0, low=100.0) for m in range(0, 30, 5)]
    bar = _bar(9, 45, high=110.0, low=105.0, close=108.0)
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None


def test_volume_filter_blocks_low_volume_break() -> None:
    s = _fixture_strategy(range_minutes=15, volume_mult=2.0, volume_lookback=5)
    cfg = _config_for_test()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0), [], 10_000.0, cfg)
    # ATR-warmup bars at avg volume 1000
    hist = [_bar(8, m, high=120.0, low=100.0, volume=1000.0) for m in range(0, 30, 5)]
    # Breakout bar at low volume — blocked
    bar = _bar(9, 45, high=125.0, low=120.0, close=124.0, volume=500.0)
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None
    # High-volume breakout — fires
    bar = _bar(9, 50, high=126.0, low=120.0, close=125.0, volume=3000.0)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None


def test_ema_bias_blocks_long_below_ema() -> None:
    """A long breakout must not fire when current price < EMA bias.

    Use a long EMA period (50) and many high-price warmup bars so
    the EMA stays well above the (low) breakout price even after
    the range bars dilute it.
    """
    s = _fixture_strategy(range_minutes=15, ema_bias_period=50, atr_period=3)
    cfg = _config_for_test()
    # 50 high-price warmup bars (one per minute, before RTH) — EMA
    # converges to ~500 after enough bars.
    base = datetime(2026, 1, 15, 5, 0, tzinfo=_NY)
    warmup_hist: list[BarData] = []
    for i in range(50):
        ts = (base + timedelta(minutes=i)).astimezone(UTC)
        bar = BarData(
            timestamp=ts,
            symbol="MNQ",
            open=500.0,
            high=500.0,
            low=500.0,
            close=500.0,
            volume=1000.0,
        )
        warmup_hist.append(bar)
        s.maybe_enter(bar, warmup_hist, 10_000.0, cfg)
    # Range bars at low price
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0, close=100.0), warmup_hist, 10_000.0, cfg)
    # Breakout: price 124 is far below EMA (~470+ even after dilution)
    bar = _bar(9, 45, high=125.0, low=120.0, close=124.0)
    out = s.maybe_enter(bar, warmup_hist, 10_000.0, cfg)
    assert out is None, f"long should be blocked by EMA bias; ema={s._ema}"


# ---------------------------------------------------------------------------
# Risk math
# ---------------------------------------------------------------------------


def test_position_size_scales_with_equity() -> None:
    s = _fixture_strategy(range_minutes=15, atr_stop_mult=1.0, atr_period=3)
    cfg = _config_for_test()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0), [], 10_000.0, cfg)
    # ATR ≈ 20 (range 100→120)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]
    bar = _bar(9, 45, high=125.0, low=120.0, close=124.0)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    # risk = 1% × 10000 = 100; stop_dist = 1.0 × ATR = 20; qty = 100/20 = 5
    assert out.qty == pytest.approx(5.0, rel=1e-6)


def test_target_distance_uses_rr_multiple() -> None:
    s = _fixture_strategy(range_minutes=15, atr_stop_mult=1.0, atr_period=3, rr_target=3.0)
    cfg = _config_for_test()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0), [], 10_000.0, cfg)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]
    bar = _bar(9, 45, high=125.0, low=120.0, close=124.0)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    # entry 124, stop_dist = 20, target should be 124 + 60 = 184
    assert out.target == pytest.approx(184.0, rel=1e-3)
    assert out.stop == pytest.approx(104.0, rel=1e-3)


def test_emits_orb_regime_tag() -> None:
    s = _fixture_strategy(range_minutes=15, atr_period=3)
    cfg = _config_for_test()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0), [], 10_000.0, cfg)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]
    bar = _bar(9, 45, high=125.0, low=120.0, close=124.0)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.regime == "orb_breakout"


# ---------------------------------------------------------------------------
# Cross-asset ES confirmation filter
# ---------------------------------------------------------------------------


def _es_bar(
    local_h: int, local_m: int, *, high: float, low: float, close: float | None = None, day: int = 15
) -> BarData:
    """Build a synthetic ES1 bar at the same NY-local time as a primary bar."""
    local_dt = datetime(2026, 1, day, local_h, local_m, tzinfo=_NY)
    utc_dt = local_dt.astimezone(UTC)
    c = close if close is not None else (high + low) / 2
    return BarData(
        timestamp=utc_dt,
        symbol="ES",
        open=(high + low) / 2,
        high=high,
        low=low,
        close=c,
        volume=5000.0,
    )


def _drive_range_with_es(
    s: ORBStrategy,
    *,
    mnq_high: float,
    mnq_low: float,
    es_high: float,
    es_low: float,
    day: int = 15,
) -> None:
    """Run the three range-window bars through the strategy with paired ES bars.

    Used by every ES-filter test to leave the strategy in 'range_complete=True'
    state with both MNQ and ES ranges populated.
    """
    cfg = _config_for_test()
    es_by_minute = {
        (h, m): _es_bar(h, m, high=es_high, low=es_low, day=day)
        for h, m in [(9, 30), (9, 35), (9, 40), (9, 45), (9, 50)]
    }

    def _provider(b: BarData) -> BarData | None:
        local_t = b.timestamp.astimezone(_NY)
        return es_by_minute.get((local_t.hour, local_t.minute))

    s.attach_es_provider(_provider)
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(
            _bar(h, m, high=mnq_high, low=mnq_low, day=day),
            [],
            10_000.0,
            cfg,
        )


def test_es_filter_off_means_no_change() -> None:
    """Default config: ES filter disabled; trade fires regardless of ES."""
    s = _fixture_strategy(range_minutes=15)
    cfg = _config_for_test()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0), [], 10_000.0, cfg)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]
    bar = _bar(9, 45, high=125.0, low=120.0, close=124.0)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None  # filter off → break wins on its own


def test_es_confirmation_long_passes_when_es_also_breaks_high() -> None:
    s = _fixture_strategy(range_minutes=15, require_es_confirmation=True)
    cfg = _config_for_test()
    _drive_range_with_es(s, mnq_high=120.0, mnq_low=100.0, es_high=4500.0, es_low=4400.0)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]

    def _provider(b: BarData) -> BarData | None:
        # ES also breaks above its range high (4500)
        return _es_bar(9, 45, high=4520.0, low=4495.0, close=4515.0)

    s.attach_es_provider(_provider)
    bar = _bar(9, 45, high=125.0, low=120.0, close=124.0)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"


def test_es_confirmation_long_blocked_when_es_does_not_break() -> None:
    """MNQ breaks high, ES stays inside its range → no trade."""
    s = _fixture_strategy(range_minutes=15, require_es_confirmation=True)
    cfg = _config_for_test()
    _drive_range_with_es(s, mnq_high=120.0, mnq_low=100.0, es_high=4500.0, es_low=4400.0)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]

    def _provider(b: BarData) -> BarData | None:
        # ES still inside range — high 4495 < 4500
        return _es_bar(9, 45, high=4495.0, low=4470.0, close=4490.0)

    s.attach_es_provider(_provider)
    bar = _bar(9, 45, high=125.0, low=120.0, close=124.0)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is None


def test_es_confirmation_short_passes_when_es_also_breaks_low() -> None:
    s = _fixture_strategy(range_minutes=15, require_es_confirmation=True)
    cfg = _config_for_test()
    _drive_range_with_es(s, mnq_high=120.0, mnq_low=100.0, es_high=4500.0, es_low=4400.0)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]

    def _provider(b: BarData) -> BarData | None:
        return _es_bar(9, 45, high=4410.0, low=4385.0, close=4390.0)

    s.attach_es_provider(_provider)
    bar = _bar(9, 45, high=100.0, low=95.0, close=96.0)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "SELL"


def test_es_confirmation_blocks_when_provider_returns_none() -> None:
    """Fail-closed: missing ES bar at trade minute → no trade."""
    s = _fixture_strategy(range_minutes=15, require_es_confirmation=True)
    cfg = _config_for_test()
    _drive_range_with_es(s, mnq_high=120.0, mnq_low=100.0, es_high=4500.0, es_low=4400.0)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]
    s.attach_es_provider(lambda _b: None)
    bar = _bar(9, 45, high=125.0, low=120.0, close=124.0)
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None


def test_es_confirmation_blocks_when_no_provider_attached() -> None:
    """Filter on but no provider attached → fail-closed (no trade)."""
    s = _fixture_strategy(range_minutes=15, require_es_confirmation=True)
    cfg = _config_for_test()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0), [], 10_000.0, cfg)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]
    bar = _bar(9, 45, high=125.0, low=120.0, close=124.0)
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None


def test_es_confirmation_provider_exception_is_isolated() -> None:
    """A flaky provider should not crash the strategy; fail-closed instead."""
    s = _fixture_strategy(range_minutes=15, require_es_confirmation=True)
    cfg = _config_for_test()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_bar(h, m, high=120.0, low=100.0), [], 10_000.0, cfg)
    hist = [_bar(8, m, high=120.0, low=100.0) for m in range(0, 30, 5)]

    def _bad(_b: BarData) -> BarData | None:
        raise RuntimeError("data lib unhappy")

    s.attach_es_provider(_bad)
    bar = _bar(9, 45, high=125.0, low=120.0, close=124.0)
    # Should not raise; should fail-closed.
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None
