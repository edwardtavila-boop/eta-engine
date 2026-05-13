"""Tests for strategies.mbt_overnight_gap_strategy.

Post-2026-05-07 pivot: the strategy is now a CONTINUATION trade
(gap-up -> LONG, gap-down -> SHORT). These tests reflect that
direction, not the legacy fade behavior.

Coverage:
* Strategy imports + constructs cleanly.
* Preset reflects post-pivot defaults.
* Empty bars produce no signal.
* Out-of-RTH bar yields no signal.
* Gap-up at NY RTH open with bullish bar fires LONG (continuation).
* Gap-down at NY RTH open with bearish bar fires SHORT (continuation).
* Trivial gap (<floor) is rejected.
* Specifically, a 0.5xATR gap is rejected at the post-pivot 1.0xATR floor.
* Weekend gap (Monday RTH after Friday close) anchors correctly on Friday.
* Red bar after gap-up does NOT fire (continuation needs same-direction bar).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.mbt_overnight_gap_strategy import (
    MBTOvernightGapConfig,
    MBTOvernightGapStrategy,
    mbt_overnight_gap_preset,
)

_CT = ZoneInfo("America/Chicago")


def _bar(
    ts: datetime,
    *,
    high: float,
    low: float,
    open_: float | None = None,
    close: float | None = None,
    volume: float = 1000.0,
) -> BarData:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_CT)
    utc_dt = ts.astimezone(UTC)
    o = open_ if open_ is not None else (high + low) / 2
    c = close if close is not None else (high + low) / 2
    return BarData(
        timestamp=utc_dt,
        symbol="MBT",
        open=o,
        high=high,
        low=low,
        close=c,
        volume=volume,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="MBT",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.005,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _permissive_cfg(**overrides: object) -> MBTOvernightGapConfig:
    base: dict[str, object] = {
        "atr_period": 5,
        "warmup_bars": 6,
        "entry_window_bars": 6,
        "min_session_gap_hours": 2.0,
    }
    base.update(overrides)
    return MBTOvernightGapConfig(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Imports + construction
# ---------------------------------------------------------------------------


def test_strategy_imports_and_initializes() -> None:
    s = MBTOvernightGapStrategy()
    assert s.cfg.allow_long is True
    assert s.cfg.allow_short is True
    assert s.stats["entries_fired"] == 0


def test_preset_constructs_cleanly_post_pivot() -> None:
    """Preset reflects post-pivot defaults: min_gap_atr_mult=1.0
    (raised from legacy 0.3 per the EDA noise floor)."""
    cfg = mbt_overnight_gap_preset()
    assert isinstance(cfg, MBTOvernightGapConfig)
    assert cfg.min_gap_atr_mult == 1.0
    assert cfg.max_gap_atr_mult == 1.5
    s = MBTOvernightGapStrategy(cfg)
    assert s.stats["bars_seen"] == 0


# ---------------------------------------------------------------------------
# No-signal cases
# ---------------------------------------------------------------------------


def test_empty_bars_produce_no_signal() -> None:
    s = MBTOvernightGapStrategy(_permissive_cfg())
    cfg = _config()
    bar = _bar(datetime(2026, 6, 15, 10, 0), high=60_000.0, low=59_900.0, close=59_950.0)
    assert s.maybe_enter(bar, [], 10_000.0, cfg) is None


def test_pre_rth_bar_yields_no_signal() -> None:
    """An out-of-session bar shouldn't fire even with a gap."""
    s = MBTOvernightGapStrategy(_permissive_cfg())
    cfg = _config()
    base = datetime(2026, 6, 15, 5, 0, tzinfo=_CT)  # 05:00 CT pre-RTH
    bar = _bar(base, high=60_000.0, low=59_900.0, close=59_950.0)
    assert s.maybe_enter(bar, [], 10_000.0, cfg) is None


# ---------------------------------------------------------------------------
# Signal case
# ---------------------------------------------------------------------------


def test_gap_up_at_rth_open_fires_long() -> None:
    """Build a prior-day session ending around 14:55 CT at 60_000, then
    gap up to 60_500 at 08:30 CT next day, with a bullish bar. The
    post-pivot thesis is continuation, so this should fire LONG.
    """
    cfg = _permissive_cfg(
        atr_period=5,
        warmup_bars=4,
        min_gap_atr_mult=0.3,
        max_gap_atr_mult=10.0,  # very permissive ceiling for test
        min_session_gap_hours=2.0,
        entry_window_bars=6,
    )
    s = MBTOvernightGapStrategy(cfg)
    bcfg = _config()

    hist: list[BarData] = []

    # Prior session: 5 bars during prior day RTH ending around 14:55 CT
    prior_day = datetime(2026, 6, 14, 14, 30, tzinfo=_CT)
    for i in range(5):
        ts = prior_day + timedelta(minutes=i * 5)
        bar = _bar(ts, high=60_050.0, low=59_950.0, close=60_000.0, volume=1000.0)
        s.maybe_enter(bar, hist, 10_000.0, bcfg)
        hist.append(bar)

    # The strategy uses hist[-1] at new-day rollover; that's our
    # last-RTH-close anchor (60_000).
    # Now next day: large time gap (overnight), gap-up open at 60_500.
    # ATR over the prior 5 bars = 100.0; gap of 500 = 5x ATR -> too
    # large under default 1.5x ceiling, so we widened max_gap_atr_mult.
    next_day_open = datetime(2026, 6, 15, 8, 30, tzinfo=_CT)
    # First bar of new RTH: gap up + bullish (close > open)
    open_bar = _bar(
        next_day_open,
        high=60_650.0,
        low=60_450.0,
        open_=60_500.0,
        close=60_600.0,
        volume=1000.0,
    )
    out = s.maybe_enter(open_bar, hist, 10_000.0, bcfg)
    # The strategy needs to register the gap on this bar; entry may
    # happen on this same bar if it qualifies. If not, push another.
    if out is None:
        hist.append(open_bar)
        # Next bar -- still inside entry window, still bullish
        next_ts = next_day_open + timedelta(minutes=5)
        next_bar = _bar(
            next_ts,
            high=60_750.0,
            low=60_550.0,
            open_=60_600.0,
            close=60_700.0,
            volume=1000.0,
        )
        out = s.maybe_enter(next_bar, hist, 10_000.0, bcfg)
    assert out is not None, f"expected LONG to fire on gap-up continuation; stats={s.stats}"
    assert out.side == "BUY"
    assert out.stop < out.entry_price
    assert out.target > out.entry_price


def test_gap_too_small_yields_no_signal() -> None:
    """A trivial gap (~0.1x ATR) below the floor should not fire."""
    cfg = _permissive_cfg(
        atr_period=5,
        warmup_bars=4,
        min_gap_atr_mult=0.5,
        max_gap_atr_mult=2.0,
        min_session_gap_hours=2.0,
    )
    s = MBTOvernightGapStrategy(cfg)
    bcfg = _config()
    hist: list[BarData] = []

    prior_day = datetime(2026, 6, 14, 14, 30, tzinfo=_CT)
    for i in range(5):
        ts = prior_day + timedelta(minutes=i * 5)
        bar = _bar(ts, high=60_050.0, low=59_950.0, close=60_000.0, volume=1000.0)
        s.maybe_enter(bar, hist, 10_000.0, bcfg)
        hist.append(bar)

    next_day_open = datetime(2026, 6, 15, 8, 30, tzinfo=_CT)
    # Tiny gap of 5 vs ATR 100 -> 0.05x ATR -> below floor
    open_bar = _bar(next_day_open, high=60_010.0, low=59_990.0, open_=60_005.0, close=59_995.0, volume=1000.0)
    out = s.maybe_enter(open_bar, hist, 10_000.0, bcfg)
    assert out is None
    assert s._n_gaps_too_small >= 1


def test_gap_down_fires_short_continuation() -> None:
    """Gap-down at NY RTH open with a red entry bar fires SHORT
    (continuation thesis)."""
    cfg = _permissive_cfg(
        atr_period=5,
        warmup_bars=4,
        min_gap_atr_mult=1.0,
        max_gap_atr_mult=10.0,
        min_session_gap_hours=2.0,
        entry_window_bars=6,
    )
    s = MBTOvernightGapStrategy(cfg)
    bcfg = _config()
    hist: list[BarData] = []

    prior_day = datetime(2026, 6, 14, 14, 30, tzinfo=_CT)
    for i in range(5):
        ts = prior_day + timedelta(minutes=i * 5)
        bar = _bar(
            ts,
            high=60_050.0,
            low=59_950.0,
            close=60_000.0,
            volume=1000.0,
        )
        s.maybe_enter(bar, hist, 10_000.0, bcfg)
        hist.append(bar)

    next_day_open = datetime(2026, 6, 15, 8, 30, tzinfo=_CT)
    # Gap DOWN to 59_500, red entry bar (close < open).
    open_bar = _bar(
        next_day_open,
        high=59_550.0,
        low=59_300.0,
        open_=59_500.0,
        close=59_400.0,
        volume=1000.0,
    )
    out = s.maybe_enter(open_bar, hist, 10_000.0, bcfg)
    if out is None:
        hist.append(open_bar)
        next_ts = next_day_open + timedelta(minutes=5)
        next_bar = _bar(
            next_ts,
            high=59_450.0,
            low=59_200.0,
            open_=59_400.0,
            close=59_250.0,
            volume=1000.0,
        )
        out = s.maybe_enter(next_bar, hist, 10_000.0, bcfg)
    assert out is not None, f"expected SHORT continuation on gap-down; stats={s.stats}"
    assert out.side == "SELL"
    assert out.stop > out.entry_price
    assert out.target < out.entry_price


def test_small_gap_below_one_atr_is_rejected() -> None:
    """At the post-pivot 1.0xATR floor, a 0.5xATR gap is rejected.

    Tests the EDA-derived noise floor: smaller gaps produce no
    continuation edge per the 70d study.
    """
    cfg = _permissive_cfg(
        atr_period=5,
        warmup_bars=4,
        min_gap_atr_mult=1.0,
        max_gap_atr_mult=2.0,
        min_session_gap_hours=2.0,
    )
    s = MBTOvernightGapStrategy(cfg)
    bcfg = _config()
    hist: list[BarData] = []

    prior_day = datetime(2026, 6, 14, 14, 30, tzinfo=_CT)
    for i in range(5):
        ts = prior_day + timedelta(minutes=i * 5)
        bar = _bar(
            ts,
            high=60_050.0,
            low=59_950.0,
            close=60_000.0,
            volume=1000.0,
        )
        s.maybe_enter(bar, hist, 10_000.0, bcfg)
        hist.append(bar)

    # Gap of 50 vs ATR 100 -> 0.5x ATR -> below 1.0 floor.
    next_day_open = datetime(2026, 6, 15, 8, 30, tzinfo=_CT)
    open_bar = _bar(
        next_day_open,
        high=60_100.0,
        low=60_020.0,
        open_=60_050.0,
        close=60_080.0,
        volume=1000.0,
    )
    out = s.maybe_enter(open_bar, hist, 10_000.0, bcfg)
    assert out is None
    assert s._n_gaps_too_small >= 1


def test_weekend_gap_monday_after_friday_close() -> None:
    """Monday RTH open after Friday RTH close: the strategy must
    correctly anchor on the Friday RTH close.

    2026-06-12 is a Friday, 2026-06-15 a Monday. The new-day branch
    walks backward through hist looking for a prior-date in-session
    bar; the Friday RTH bars are the natural anchor source.
    """
    cfg = _permissive_cfg(
        atr_period=5,
        warmup_bars=4,
        min_gap_atr_mult=1.0,
        max_gap_atr_mult=10.0,
        # Weekends easily clear any reasonable session-gap floor.
        min_session_gap_hours=2.0,
        entry_window_bars=6,
    )
    s = MBTOvernightGapStrategy(cfg)
    bcfg = _config()

    hist: list[BarData] = []
    fri_session_start = datetime(2026, 6, 12, 14, 0, tzinfo=_CT)
    for i in range(5):
        ts = fri_session_start + timedelta(minutes=i * 5)
        bar = _bar(
            ts,
            high=60_050.0,
            low=59_950.0,
            close=60_000.0,
            volume=1000.0,
        )
        s.maybe_enter(bar, hist, 10_000.0, bcfg)
        hist.append(bar)

    # Monday RTH open at 08:30 CT -- gap UP across the weekend.
    mon_open = datetime(2026, 6, 15, 8, 30, tzinfo=_CT)
    open_bar = _bar(
        mon_open,
        high=60_700.0,
        low=60_450.0,
        open_=60_500.0,
        close=60_650.0,
        volume=1000.0,
    )
    out = s.maybe_enter(open_bar, hist, 10_000.0, bcfg)
    if out is None:
        hist.append(open_bar)
        next_ts = mon_open + timedelta(minutes=5)
        next_bar = _bar(
            next_ts,
            high=60_750.0,
            low=60_600.0,
            open_=60_650.0,
            close=60_720.0,
            volume=1000.0,
        )
        out = s.maybe_enter(next_bar, hist, 10_000.0, bcfg)
    assert out is not None, f"expected weekend-gap LONG continuation; stats={s.stats}"
    assert out.side == "BUY"
    # Sanity: the strategy used a Friday RTH bar as the anchor.
    assert s._last_rth_close == 60_000.0


def test_red_bar_after_gap_up_does_not_fire() -> None:
    """Continuation needs the entry bar to close in the gap direction.

    A red bar after a gap-up (potential exhaustion) must NOT fire
    LONG. Under the legacy FADE thesis this exact bar would have
    fired SHORT -- this is the smoking-gun test that direction was
    inverted correctly.
    """
    cfg = _permissive_cfg(
        atr_period=5,
        warmup_bars=4,
        min_gap_atr_mult=1.0,
        max_gap_atr_mult=10.0,
        min_session_gap_hours=2.0,
        entry_window_bars=1,  # only first bar eligible
    )
    s = MBTOvernightGapStrategy(cfg)
    bcfg = _config()

    hist: list[BarData] = []
    prior_day = datetime(2026, 6, 14, 14, 0, tzinfo=_CT)
    for i in range(5):
        ts = prior_day + timedelta(minutes=i * 5)
        bar = _bar(
            ts,
            high=60_050.0,
            low=59_950.0,
            close=60_000.0,
            volume=1000.0,
        )
        s.maybe_enter(bar, hist, 10_000.0, bcfg)
        hist.append(bar)

    next_day_open = datetime(2026, 6, 15, 8, 30, tzinfo=_CT)
    # Gap up but RED bar: close < open. Continuation rule rejects.
    red_open_bar = _bar(
        next_day_open,
        high=60_550.0,
        low=60_300.0,
        open_=60_500.0,
        close=60_350.0,
        volume=1000.0,
    )
    out = s.maybe_enter(red_open_bar, hist, 10_000.0, bcfg)
    assert out is None, f"continuation should reject red-bar after gap-up; stats={s.stats}"
