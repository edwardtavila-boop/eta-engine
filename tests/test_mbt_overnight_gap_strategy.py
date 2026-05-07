"""Tests for strategies.mbt_overnight_gap_strategy.

Coverage:
* Strategy imports + constructs cleanly.
* Empty bars produce no signal.
* Synthetic gap-up at NY RTH open with bearish bar produces SHORT.
* Out-of-RTH bar yields no signal.
* Preset constructs cleanly.
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
        timestamp=utc_dt, symbol="MBT", open=o, high=high, low=low,
        close=c, volume=volume,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="MBT", initial_equity=10_000.0,
        risk_per_trade_pct=0.005, confluence_threshold=0.0,
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


def test_preset_constructs_cleanly() -> None:
    cfg = mbt_overnight_gap_preset()
    assert isinstance(cfg, MBTOvernightGapConfig)
    s = MBTOvernightGapStrategy(cfg)
    assert s.stats["bars_seen"] == 0


# ---------------------------------------------------------------------------
# No-signal cases
# ---------------------------------------------------------------------------


def test_empty_bars_produce_no_signal() -> None:
    s = MBTOvernightGapStrategy(_permissive_cfg())
    cfg = _config()
    bar = _bar(datetime(2026, 6, 15, 10, 0), high=60_000.0, low=59_900.0,
               close=59_950.0)
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


def test_gap_up_at_rth_open_fires_short() -> None:
    """Build a prior-day session ending around 14:55 CT at 60_000, then
    gap up to 60_500 at 08:30 CT next day, with a bearish bar — should
    fire SHORT.
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
        bar = _bar(ts, high=60_050.0, low=59_950.0, close=60_000.0,
                   volume=1000.0)
        s.maybe_enter(bar, hist, 10_000.0, bcfg)
        hist.append(bar)

    # The strategy uses hist[-1] at new-day rollover; that's our
    # last-RTH-close anchor (60_000).
    # Now next day: large time gap (overnight), gap-up open at 60_500.
    # ATR over the prior 5 bars = 100.0; gap of 500 = 5x ATR -> too
    # large under default 1.5x ceiling, so we widened max_gap_atr_mult.
    next_day_open = datetime(2026, 6, 15, 8, 30, tzinfo=_CT)
    # First bar of new RTH: gap up + bearish (close < open)
    open_bar = _bar(
        next_day_open,
        high=60_550.0, low=60_300.0,
        open_=60_500.0, close=60_350.0, volume=1000.0,
    )
    out = s.maybe_enter(open_bar, hist, 10_000.0, bcfg)
    # The strategy needs to register the gap on this bar; entry may
    # happen on this same bar if it qualifies. If not, push another.
    if out is None:
        hist.append(open_bar)
        # Next bar — still inside entry window, still bearish
        next_ts = next_day_open + timedelta(minutes=5)
        next_bar = _bar(
            next_ts, high=60_400.0, low=60_200.0,
            open_=60_350.0, close=60_250.0, volume=1000.0,
        )
        out = s.maybe_enter(next_bar, hist, 10_000.0, bcfg)
    assert out is not None, (
        f"expected SHORT to fire on gap-up; stats={s.stats}"
    )
    assert out.side == "SELL"
    assert out.stop > out.entry_price
    assert out.target < out.entry_price


def test_gap_too_small_yields_no_signal() -> None:
    """A trivial gap (~0.1x ATR) below the floor should not fire."""
    cfg = _permissive_cfg(
        atr_period=5, warmup_bars=4,
        min_gap_atr_mult=0.5, max_gap_atr_mult=2.0,
        min_session_gap_hours=2.0,
    )
    s = MBTOvernightGapStrategy(cfg)
    bcfg = _config()
    hist: list[BarData] = []

    prior_day = datetime(2026, 6, 14, 14, 30, tzinfo=_CT)
    for i in range(5):
        ts = prior_day + timedelta(minutes=i * 5)
        bar = _bar(ts, high=60_050.0, low=59_950.0, close=60_000.0,
                   volume=1000.0)
        s.maybe_enter(bar, hist, 10_000.0, bcfg)
        hist.append(bar)

    next_day_open = datetime(2026, 6, 15, 8, 30, tzinfo=_CT)
    # Tiny gap of 5 vs ATR 100 -> 0.05x ATR -> below floor
    open_bar = _bar(next_day_open, high=60_010.0, low=59_990.0,
                    open_=60_005.0, close=59_995.0, volume=1000.0)
    out = s.maybe_enter(open_bar, hist, 10_000.0, bcfg)
    assert out is None
    assert s._n_gaps_too_small >= 1
