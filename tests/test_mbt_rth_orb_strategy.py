"""Unit tests for MBT 5m RTH ORB strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from eta_engine.strategies.mbt_rth_orb_strategy import (
    MBTRTHORBStrategy,
    mbt_rth_orb_preset,
)

_CT = ZoneInfo("America/Chicago")


@dataclass
class _Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class _Cfg:
    """Stub BacktestConfig — strategy doesn't read it."""

    pass


def _bar(
    hour_ct: int, minute_ct: int, *, high: float, low: float, close: float | None = None, volume: float = 100.0
) -> _Bar:
    """Build a 5m bar at the given CT hour/minute."""
    base = datetime(2026, 5, 1, tzinfo=_CT).astimezone(UTC).date()
    ct_dt = datetime.combine(base, time(hour_ct, minute_ct), tzinfo=_CT)
    return _Bar(
        timestamp=ct_dt.astimezone(UTC),
        open=close or (high + low) / 2,
        high=high,
        low=low,
        close=close or (high + low) / 2,
        volume=volume,
    )


def _bar_at(
    date_ct, hour_ct: int, minute_ct: int, *, high: float, low: float, close: float | None = None, volume: float = 100.0
) -> _Bar:
    ct_dt = datetime.combine(date_ct, time(hour_ct, minute_ct), tzinfo=_CT)
    return _Bar(
        timestamp=ct_dt.astimezone(UTC),
        open=close or (high + low) / 2,
        high=high,
        low=low,
        close=close or (high + low) / 2,
        volume=volume,
    )


# --- import + init ---------------------------------------------------------


def test_import_and_init() -> None:
    s = MBTRTHORBStrategy()
    assert s.cfg.range_minutes == 5
    assert s.cfg.rr_target == 3.0
    assert s.cfg.min_range_pts == 245.0
    assert s.stats == {
        "breakouts_seen": 0,
        "min_range_rejects": 0,
        "volume_rejects": 0,
        "entries_fired": 0,
    }


def test_preset_matches_eda_derivation() -> None:
    """The shipped preset must exactly match the EDA-derived defaults."""
    preset = mbt_rth_orb_preset()
    assert preset.min_range_pts == 245.0
    assert preset.rr_target == 3.0
    assert preset.atr_stop_mult == 1.0
    assert preset.range_minutes == 5
    assert preset.timezone_name == "America/Chicago"


def test_pre_rth_returns_none() -> None:
    s = MBTRTHORBStrategy()
    cfg = _Cfg()
    # Pre-RTH bar at 06:00 CT
    bar = _bar(6, 0, high=80_000, low=79_500, close=79_800)
    out = s.maybe_enter(bar, [], 50_000.0, cfg)
    assert out is None


def test_in_range_window_returns_none() -> None:
    """Bars during the 5m opening-range window return None — they
    contribute to the range, they don't trigger entries."""
    s = MBTRTHORBStrategy()
    cfg = _Cfg()
    out = s.maybe_enter(_bar(8, 30, high=80_500, low=80_000, close=80_200), [], 50_000.0, cfg)
    assert out is None
    assert s._day is not None
    assert s._day.range_high == 80_500
    assert s._day.range_low == 80_000


def test_post_range_no_breakout_returns_none() -> None:
    s = MBTRTHORBStrategy()
    cfg = _Cfg()
    s.maybe_enter(_bar(8, 30, high=80_500, low=80_000, close=80_200), [], 50_000.0, cfg)
    # 08:35 CT: range complete; bar inside the range → no entry
    # 14-bar ATR window of synthetic 5m bars
    hist = [_bar(7 + (25 + 5 * i) // 60, (25 + 5 * i) % 60, high=80_300, low=80_100) for i in range(15)]
    in_range = _bar(8, 35, high=80_400, low=80_100, close=80_200)
    out = s.maybe_enter(in_range, hist, 50_000.0, cfg)
    assert out is None


def test_min_range_filter_rejects_dead_open() -> None:
    """A 100pt opening range (well below the 245pt EDA p25 threshold)
    should be rejected even on a real breakout."""
    s = MBTRTHORBStrategy()
    cfg = _Cfg()
    # 5m range bar with only 100pt range — below threshold
    s.maybe_enter(_bar(8, 30, high=80_100, low=80_000, close=80_050), [], 50_000.0, cfg)
    hist = [_bar(7 + (25 + 5 * i) // 60, (25 + 5 * i) % 60, high=80_300, low=80_100) for i in range(15)]
    breakout = _bar(8, 35, high=80_300, low=80_050, close=80_200)
    out = s.maybe_enter(breakout, hist, 50_000.0, cfg)
    assert out is None
    assert s._n_min_range_rejects == 1


def test_long_breakout_above_range_high_fires() -> None:
    """Range with sufficient width + close above range high → LONG entry."""
    s = MBTRTHORBStrategy()
    cfg = _Cfg()
    # 5m range bar with 300pt range (above 245 threshold)
    s.maybe_enter(_bar(8, 30, high=80_300, low=80_000, close=80_150), [], 50_000.0, cfg)
    hist = [_bar(7 + (25 + 5 * i) // 60, (25 + 5 * i) % 60, high=80_400, low=80_100) for i in range(15)]
    # Breakout bar pushes above range_high (80_300)
    breakout = _bar(8, 35, high=80_500, low=80_200, close=80_450)
    out = s.maybe_enter(breakout, hist, 50_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert out.entry_price == 80_450
    # Stop should be below entry, target above
    assert out.stop < out.entry_price
    assert out.target > out.entry_price
    # 3R target: target distance = 3 * stop distance
    stop_dist = out.entry_price - out.stop
    target_dist = out.target - out.entry_price
    assert pytest.approx(target_dist / stop_dist, rel=0.1) == 3.0


def test_short_breakout_below_range_low_fires() -> None:
    s = MBTRTHORBStrategy()
    cfg = _Cfg()
    s.maybe_enter(_bar(8, 30, high=80_300, low=80_000, close=80_150), [], 50_000.0, cfg)
    hist = [_bar(7 + (25 + 5 * i) // 60, (25 + 5 * i) % 60, high=80_400, low=80_100) for i in range(15)]
    # Breakdown bar pushes below range_low (80_000)
    breakdown = _bar(8, 35, high=80_200, low=79_800, close=79_900)
    out = s.maybe_enter(breakdown, hist, 50_000.0, cfg)
    assert out is not None
    assert out.side == "SELL"


def test_one_trade_per_day_cap() -> None:
    """After firing once, no second entry on the same day."""
    s = MBTRTHORBStrategy()
    cfg = _Cfg()
    s.maybe_enter(_bar(8, 30, high=80_300, low=80_000, close=80_150), [], 50_000.0, cfg)
    hist = [_bar(7 + (25 + 5 * i) // 60, (25 + 5 * i) % 60, high=80_400, low=80_100) for i in range(15)]
    first = _bar(8, 35, high=80_500, low=80_200, close=80_450)
    second = _bar(8, 40, high=80_700, low=80_400, close=80_650)
    out1 = s.maybe_enter(first, hist, 50_000.0, cfg)
    out2 = s.maybe_enter(second, hist, 50_000.0, cfg)
    assert out1 is not None
    assert out2 is None


def test_max_entry_cutoff_at_11_ct() -> None:
    """No entries after 11:00 CT even if a breakout happens."""
    s = MBTRTHORBStrategy()
    cfg = _Cfg()
    s.maybe_enter(_bar(8, 30, high=80_300, low=80_000, close=80_150), [], 50_000.0, cfg)
    hist = [_bar(7 + (25 + 5 * i) // 60, (25 + 5 * i) % 60, high=80_400, low=80_100) for i in range(15)]
    # 11:30 CT — past the max_entry_local cutoff
    late = _bar(11, 30, high=80_500, low=80_200, close=80_450)
    out = s.maybe_enter(late, hist, 50_000.0, cfg)
    assert out is None


def test_qty_uses_point_value_not_just_price() -> None:
    """Sizing math must multiply stop_dist by point_value=0.10, NOT
    just divide by stop_dist. Without the multiplier the strategy
    would size 10x larger than intended."""
    s = MBTRTHORBStrategy()
    cfg = _Cfg()
    s.maybe_enter(_bar(8, 30, high=80_300, low=80_000, close=80_150), [], 50_000.0, cfg)
    # Construct deterministic ATR: 14 bars each with range=200 → ATR=200
    hist = [_bar(7 + (25 + 5 * i) // 60, (25 + 5 * i) % 60, high=80_300, low=80_100) for i in range(14)]
    breakout = _bar(8, 35, high=80_500, low=80_200, close=80_450)
    out = s.maybe_enter(breakout, hist, 50_000.0, cfg)
    assert out is not None
    # risk_usd = 0.5% * 50_000 = $250.
    # stop_dist ≈ 200 price points.
    # MBT point_value=0.10 ⟹ $-per-contract for stop = 200 * 0.10 = $20/contract
    # qty = $250 / $20 = 12.5
    # (Supervisor _MAX_QTY_PER_ORDER["MBT"]=3 clamps this downstream.)
    assert pytest.approx(out.qty, rel=0.05) == 12.5


def test_day_rollover_resets_state() -> None:
    """A new day re-arms the strategy."""
    s = MBTRTHORBStrategy()
    cfg = _Cfg()
    day1 = datetime(2026, 5, 1).date()
    day2 = datetime(2026, 5, 2).date()
    # Day 1: range
    s.maybe_enter(_bar_at(day1, 8, 30, high=80_300, low=80_000), [], 50_000.0, cfg)
    hist1 = [_bar_at(day1, 7 + (25 + 5 * i) // 60, (25 + 5 * i) % 60, high=80_400, low=80_100) for i in range(14)]
    s.maybe_enter(_bar_at(day1, 8, 35, high=80_500, low=80_200, close=80_450), hist1, 50_000.0, cfg)
    assert s._day is not None
    assert s._day.breakout_taken is True
    # Day 2: state must reset
    s.maybe_enter(_bar_at(day2, 8, 30, high=80_500, low=80_200, close=80_350), [], 50_000.0, cfg)
    assert s._day.breakout_taken is False
    assert s._day.trades_today == 0


def test_zero_atr_returns_none() -> None:
    """If ATR is 0 (flat-line history) the strategy must NOT divide-by-zero."""
    s = MBTRTHORBStrategy()
    cfg = _Cfg()
    s.maybe_enter(_bar(8, 30, high=80_300, low=80_000, close=80_150), [], 50_000.0, cfg)
    # 14 flat bars → ATR=0
    hist = [_bar(7 + (25 + 5 * i) // 60, (25 + 5 * i) % 60, high=80_200, low=80_200, close=80_200) for i in range(14)]
    breakout = _bar(8, 35, high=80_500, low=80_200, close=80_450)
    out = s.maybe_enter(breakout, hist, 50_000.0, cfg)
    assert out is None
