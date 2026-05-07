"""Tests for strategies.met_rth_orb_strategy.

Coverage:
* Strategy imports + constructs cleanly.
* Empty bar produces no signal.
* Building the opening range produces no signal.
* Long breakout above the range high fires BUY.
* Short breakout below the range low fires SELL.
* Out-of-session bar yields no signal.
* Preset constructs cleanly.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.met_rth_orb_strategy import (
    METRTHORBConfig,
    METRTHORBStrategy,
    met_rth_orb_preset,
)

_CT = ZoneInfo("America/Chicago")


def _bar(
    h: int,
    m: int,
    *,
    high: float,
    low: float,
    open_: float | None = None,
    close: float | None = None,
    volume: float = 1000.0,
    day: int = 15,
) -> BarData:
    """Construct a 5m bar at Chicago local time on 2026-06-DD."""
    local_dt = datetime(2026, 6, day, h, m, tzinfo=_CT)
    utc_dt = local_dt.astimezone(UTC)
    o = open_ if open_ is not None else (high + low) / 2
    c = close if close is not None else (high + low) / 2
    return BarData(
        timestamp=utc_dt, symbol="MET", open=o, high=high, low=low,
        close=c, volume=volume,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="MET", initial_equity=10_000.0,
        risk_per_trade_pct=0.005, confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _permissive_cfg(**overrides: object) -> METRTHORBConfig:
    base: dict[str, object] = {
        "range_minutes": 5,
        "ema_bias_period": 0,
        "volume_mult": 0.0,
        "atr_period": 5,
        "min_range_pts": 0.0,
    }
    base.update(overrides)
    return METRTHORBConfig(**base)  # type: ignore[arg-type]


def _strategy(**overrides: object) -> METRTHORBStrategy:
    return METRTHORBStrategy(_permissive_cfg(**overrides))


# ---------------------------------------------------------------------------
# Imports + construction
# ---------------------------------------------------------------------------


def test_strategy_imports_and_initializes() -> None:
    s = METRTHORBStrategy()
    assert s.cfg.range_minutes == 5
    assert s.stats["entries_fired"] == 0


def test_preset_constructs_cleanly() -> None:
    cfg = met_rth_orb_preset()
    assert isinstance(cfg, METRTHORBConfig)
    assert cfg.timezone_name == "America/Chicago"
    s = METRTHORBStrategy(cfg)
    assert s.stats["breakouts_seen"] == 0


# ---------------------------------------------------------------------------
# No-signal cases
# ---------------------------------------------------------------------------


def test_no_entry_during_range_window() -> None:
    """The single 5m range bar (08:30-08:35) should yield no signal."""
    s = _strategy()
    cfg = _config()
    bar = _bar(8, 30, high=2_500.0, low=2_490.0, close=2_495.0)
    assert s.maybe_enter(bar, [], 10_000.0, cfg) is None


def test_pre_rth_bar_yields_no_signal() -> None:
    s = _strategy()
    cfg = _config()
    # 03:00 CT — pre-RTH
    bar = _bar(3, 0, high=2_500.0, low=2_490.0, close=2_495.0)
    assert s.maybe_enter(bar, [], 10_000.0, cfg) is None


def test_no_breakout_inside_range() -> None:
    """Bar fully inside the range yields no signal."""
    s = _strategy(range_minutes=5)
    cfg = _config()
    # Build range with 1 bar at 08:30-08:35: high=2500, low=2480
    s.maybe_enter(_bar(8, 30, high=2_500.0, low=2_480.0), [], 10_000.0, cfg)
    # ATR history
    hist = [
        _bar(7, m, high=2_500.0, low=2_480.0)
        for m in range(0, 25, 5)
    ]
    # Bar fully inside range
    bar = _bar(8, 35, high=2_495.0, low=2_485.0, close=2_490.0)
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None


# ---------------------------------------------------------------------------
# Signal cases
# ---------------------------------------------------------------------------


def test_long_breakout_fires_buy() -> None:
    """High > range high in long-bias context fires BUY."""
    s = _strategy(range_minutes=5)
    cfg = _config()
    # Build range
    s.maybe_enter(_bar(8, 30, high=2_500.0, low=2_480.0), [], 10_000.0, cfg)
    hist = [
        _bar(7, m, high=2_500.0, low=2_480.0)
        for m in range(0, 25, 5)
    ]
    # Breakout bar at 08:35: high=2510 > 2500
    bar = _bar(8, 35, high=2_510.0, low=2_500.0, close=2_508.0)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert out.entry_price == 2_508.0
    assert out.stop < out.entry_price
    assert out.target > out.entry_price
    # Tick-quantized to 0.50
    assert (out.stop * 2) == pytest.approx(round(out.stop * 2))
    assert (out.target * 2) == pytest.approx(round(out.target * 2))


def test_short_breakout_fires_sell() -> None:
    """Low < range low fires SELL."""
    s = _strategy(range_minutes=5)
    cfg = _config()
    s.maybe_enter(_bar(8, 30, high=2_500.0, low=2_480.0), [], 10_000.0, cfg)
    hist = [
        _bar(7, m, high=2_500.0, low=2_480.0)
        for m in range(0, 25, 5)
    ]
    bar = _bar(8, 35, high=2_482.0, low=2_470.0, close=2_472.0)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "SELL"
    assert out.stop > out.entry_price
    assert out.target < out.entry_price


def test_only_one_trade_per_day() -> None:
    """Hitting max_trades_per_day blocks subsequent breakouts."""
    s = _strategy(range_minutes=5, max_trades_per_day=1)
    cfg = _config()
    s.maybe_enter(_bar(8, 30, high=2_500.0, low=2_480.0), [], 10_000.0, cfg)
    hist = [
        _bar(7, m, high=2_500.0, low=2_480.0)
        for m in range(0, 25, 5)
    ]
    out1 = s.maybe_enter(
        _bar(8, 35, high=2_510.0, low=2_500.0, close=2_508.0),
        hist, 10_000.0, cfg,
    )
    out2 = s.maybe_enter(
        _bar(8, 40, high=2_515.0, low=2_508.0, close=2_512.0),
        hist, 10_000.0, cfg,
    )
    assert out1 is not None
    assert out2 is None


def test_no_entry_after_max_entry_local() -> None:
    """Bars past max_entry_local yield no signal."""
    s = _strategy(range_minutes=5, max_entry_local=time(11, 0))
    cfg = _config()
    s.maybe_enter(_bar(8, 30, high=2_500.0, low=2_480.0), [], 10_000.0, cfg)
    hist = [
        _bar(7, m, high=2_500.0, low=2_480.0)
        for m in range(0, 25, 5)
    ]
    # 11:00 CT — exactly the cutoff, blocked
    bar = _bar(11, 0, high=2_510.0, low=2_500.0, close=2_508.0)
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None


def test_state_resets_next_day() -> None:
    """Next-day bars reset the per-day state cleanly."""
    s = _strategy(range_minutes=5)
    cfg = _config()
    # Day 15 — fire trade
    s.maybe_enter(
        _bar(8, 30, high=2_500.0, low=2_480.0, day=15), [], 10_000.0, cfg,
    )
    hist = [
        _bar(7, m, high=2_500.0, low=2_480.0, day=15)
        for m in range(0, 25, 5)
    ]
    out = s.maybe_enter(
        _bar(8, 35, high=2_510.0, low=2_500.0, close=2_508.0, day=15),
        hist, 10_000.0, cfg,
    )
    assert out is not None
    # Day 16 — fresh range; range bar yields no signal
    out_next = s.maybe_enter(
        _bar(8, 30, high=2_550.0, low=2_530.0, day=16), hist, 10_000.0, cfg,
    )
    assert out_next is None


def test_min_range_blocks_narrow_open() -> None:
    """Narrow opening range below min_range_pts is rejected."""
    s = _strategy(range_minutes=5, min_range_pts=10.0)
    cfg = _config()
    # Range width = 5
    s.maybe_enter(_bar(8, 30, high=2_485.0, low=2_480.0), [], 10_000.0, cfg)
    hist = [
        _bar(7, m, high=2_485.0, low=2_480.0)
        for m in range(0, 25, 5)
    ]
    bar = _bar(8, 35, high=2_490.0, low=2_485.0, close=2_488.0)
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None


def test_position_size_scales_with_equity() -> None:
    """qty = risk_usd / stop_dist; verify the math."""
    s = _strategy(range_minutes=5, atr_period=5, atr_stop_mult=1.0)
    cfg = _config()
    s.maybe_enter(_bar(8, 30, high=2_500.0, low=2_480.0), [], 10_000.0, cfg)
    # ATR ≈ 20
    hist = [
        _bar(7, m, high=2_500.0, low=2_480.0)
        for m in range(0, 25, 5)
    ]
    bar = _bar(8, 35, high=2_510.0, low=2_500.0, close=2_508.0)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    # risk_usd = 0.5% × 10_000 = $50.
    # stop_dist ≈ 20 price points; MET point_value=0.10 ⟹ $-per-contract
    # for that stop = 20 × 0.10 = $2/contract.
    # qty = $50 / $2 = 25 contracts (clamped downstream by the supervisor's
    # _MAX_QTY_PER_ORDER["MET"]=3 cap — the strategy emits 25 here and the
    # supervisor enforces the position cap before the order goes to IBKR).
    assert out.qty == pytest.approx(25.0, rel=0.01)

