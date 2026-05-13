"""Tests for strategies.anchor_sweep_strategy — named-anchor sweep+reclaim
on MNQ/NQ futures.

Coverage:
* Anchor state machine transitions (PDH/PDL/PMH/PML/ONH/ONL) fire
  at the correct ET session boundaries.
* Sweep + reclaim of PDH yields a SHORT signal.
* Sweep + reclaim of PDL yields a LONG signal.
* max_trades_per_day cap is enforced.
* Generated signals pass the realistic-fill signal validator.
* MNQ + NQ presets construct cleanly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.feeds.signal_validator import validate_signal
from eta_engine.strategies.anchor_sweep_strategy import (
    AnchorSweepConfig,
    AnchorSweepStrategy,
    mnq_anchor_sweep_preset,
    nq_anchor_sweep_preset,
)

_NY = ZoneInfo("America/New_York")


def _bar(
    et_dt: datetime,
    *,
    high: float,
    low: float,
    open_: float | None = None,
    close: float | None = None,
    volume: float = 1000.0,
) -> BarData:
    """Build a BarData at a New-York-local datetime."""
    if et_dt.tzinfo is None:
        et_dt = et_dt.replace(tzinfo=_NY)
    utc_dt = et_dt.astimezone(UTC)
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


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="MNQ",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.005,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _permissive_cfg(**overrides) -> AnchorSweepConfig:
    """Test config: no warmup gates beyond what the engine needs."""
    base = {
        "min_wick_pct": 0.10,
        "min_volume_z": 0.0,
        "atr_period": 5,
        "min_bars_between_trades": 0,
        "max_trades_per_day": 99,
    }
    base.update(overrides)
    return AnchorSweepConfig(**base)


# ---------------------------------------------------------------------------
# Session-boundary state machine
# ---------------------------------------------------------------------------


def test_session_bucket_classification() -> None:
    s = AnchorSweepStrategy(_permissive_cfg())
    from datetime import time as dtime

    # 02:00 ET -> overnight
    assert s._bucket_for(dtime(2, 0)) == "ON"
    # 04:00 ET -> premarket
    assert s._bucket_for(dtime(4, 0)) == "PM"
    # 09:30 ET -> RTH
    assert s._bucket_for(dtime(9, 30)) == "RTH"
    # 15:30 ET -> still RTH
    assert s._bucket_for(dtime(15, 30)) == "RTH"
    # 16:00 ET -> POST
    assert s._bucket_for(dtime(16, 0)) == "POST"
    # 18:00 ET -> overnight
    assert s._bucket_for(dtime(18, 0)) == "ON"
    # 22:30 ET -> overnight
    assert s._bucket_for(dtime(22, 30)) == "ON"


def test_premarket_freeze_at_rth_open() -> None:
    """PMH/PML get frozen at 09:30 ET when bar transitions PM → RTH."""
    s = AnchorSweepStrategy(_permissive_cfg())
    cfg = _config()
    # Premarket bars 04:00 - 09:25 with high=110, low=95
    pm_bars = [
        _bar(datetime(2026, 1, 15, 5, 0), high=110.0, low=100.0),
        _bar(datetime(2026, 1, 15, 7, 0), high=105.0, low=95.0),
        _bar(datetime(2026, 1, 15, 9, 25), high=108.0, low=102.0),
    ]
    for b in pm_bars:
        s.maybe_enter(b, list(pm_bars), 10_000.0, cfg)
    # PMH/PML should NOT yet be frozen; they're still live PM extremes
    assert s._state.pmh is None
    assert s._state.pml is None
    assert s._state.pm_high_today == 110.0
    assert s._state.pm_low_today == 95.0
    # Cross into RTH at 09:30 — boundary triggers freeze
    s.maybe_enter(
        _bar(datetime(2026, 1, 15, 9, 30), high=109.0, low=103.0),
        pm_bars,
        10_000.0,
        cfg,
    )
    assert s._state.pmh == 110.0
    assert s._state.pml == 95.0


def test_overnight_freeze_at_premarket_open() -> None:
    """ONH/ONL get frozen at 04:00 ET when bar transitions ON → PM."""
    s = AnchorSweepStrategy(_permissive_cfg())
    cfg = _config()
    # Build a fake "yesterday RTH" so the day rollover doesn't
    # interfere — kick state.current_et_date by feeding bars
    yesterday_evening = _bar(
        datetime(2026, 1, 14, 19, 0),
        high=120.0,
        low=115.0,
    )
    s.maybe_enter(yesterday_evening, [yesterday_evening], 10_000.0, cfg)
    # Overnight bars 18:00 (prev) - 04:00 today
    on_bars = [
        _bar(datetime(2026, 1, 14, 22, 0), high=125.0, low=118.0),
        _bar(datetime(2026, 1, 15, 1, 0), high=130.0, low=110.0),
        _bar(datetime(2026, 1, 15, 3, 30), high=128.0, low=115.0),
    ]
    hist = [yesterday_evening]
    for b in on_bars:
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Live overnight bucket should be tracking the extremes
    assert s._state.on_high_today == 130.0
    assert s._state.on_low_today == 110.0
    # ONH/ONL not yet frozen
    assert s._state.onh is None
    assert s._state.onl is None
    # Cross into PM at 04:00 → freeze ON
    pm_bar = _bar(datetime(2026, 1, 15, 4, 0), high=129.0, low=120.0)
    hist.append(pm_bar)
    s.maybe_enter(pm_bar, hist, 10_000.0, cfg)
    assert s._state.onh == 130.0
    assert s._state.onl == 110.0


def test_pdh_pdl_carry_forward_on_new_day() -> None:
    """Today's RTH high/low becomes tomorrow's PDH/PDL on the date roll."""
    s = AnchorSweepStrategy(_permissive_cfg())
    cfg = _config()
    # Day 1 RTH bars
    rth_bars = [
        _bar(datetime(2026, 1, 15, 10, 0), high=200.0, low=195.0),
        _bar(datetime(2026, 1, 15, 12, 0), high=205.0, low=190.0),
        _bar(datetime(2026, 1, 15, 15, 0), high=203.0, low=192.0),
    ]
    hist: list[BarData] = []
    for b in rth_bars:
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    assert s._state.rth_high_today == 205.0
    assert s._state.rth_low_today == 190.0
    # Roll to next day — feed an overnight bar in the new ET date
    next_day_overnight = _bar(
        datetime(2026, 1, 16, 1, 0),
        high=199.0,
        low=193.0,
    )
    hist.append(next_day_overnight)
    s.maybe_enter(next_day_overnight, hist, 10_000.0, cfg)
    # PDH/PDL should now equal yesterday's RTH high/low
    assert s._state.pdh == 205.0
    assert s._state.pdl == 190.0


# ---------------------------------------------------------------------------
# Sweep / reclaim direction
# ---------------------------------------------------------------------------


def _seed_pdh_pdl(s: AnchorSweepStrategy, cfg, *, pdh: float, pdl: float) -> list[BarData]:
    """Build state so PDH/PDL are populated, returning the warmup history.

    Day 1 RTH session establishes pdh/pdl, day 2 overnight bar
    triggers the carry-forward.
    """
    hist: list[BarData] = []
    # Day 1 RTH — set high/low precisely
    day1_open = _bar(
        datetime(2026, 1, 15, 9, 30),
        high=(pdh + pdl) / 2 + 0.5,
        low=(pdh + pdl) / 2 - 0.5,
    )
    hist.append(day1_open)
    s.maybe_enter(day1_open, hist, 10_000.0, cfg)

    day1_high_bar = _bar(
        datetime(2026, 1, 15, 10, 0),
        high=pdh,
        low=pdh - 5.0,
    )
    hist.append(day1_high_bar)
    s.maybe_enter(day1_high_bar, hist, 10_000.0, cfg)

    day1_low_bar = _bar(
        datetime(2026, 1, 15, 12, 0),
        high=pdl + 5.0,
        low=pdl,
    )
    hist.append(day1_low_bar)
    s.maybe_enter(day1_low_bar, hist, 10_000.0, cfg)

    day1_close = _bar(
        datetime(2026, 1, 15, 15, 55),
        high=(pdh + pdl) / 2 + 1.0,
        low=(pdh + pdl) / 2 - 1.0,
    )
    hist.append(day1_close)
    s.maybe_enter(day1_close, hist, 10_000.0, cfg)
    # Day 2 overnight bar — triggers PDH/PDL carry-forward
    next_overnight = _bar(
        datetime(2026, 1, 16, 0, 30),
        high=(pdh + pdl) / 2,
        low=(pdh + pdl) / 2 - 0.5,
    )
    hist.append(next_overnight)
    s.maybe_enter(next_overnight, hist, 10_000.0, cfg)
    return hist


def test_sweep_pdh_produces_short_signal() -> None:
    """High pierces PDH and close reclaims back below → SELL signal."""
    s = AnchorSweepStrategy(_permissive_cfg())
    cfg = _config()
    pdh = 200.0
    pdl = 190.0
    hist = _seed_pdh_pdl(s, cfg, pdh=pdh, pdl=pdl)
    assert s._state.pdh == pdh
    assert s._state.pdl == pdl

    # Sweep bar: high=201 (above pdh=200), close=199.5 (back below).
    # wick above level = 1.0, total range = 3.0 → wick_pct ≈ 0.33 OK.
    sweep_bar = _bar(
        datetime(2026, 1, 16, 10, 0),
        high=201.0,
        low=198.0,
        open_=199.0,
        close=199.5,
        volume=2000.0,
    )
    hist.append(sweep_bar)
    out = s.maybe_enter(sweep_bar, hist, 10_000.0, cfg)
    assert out is not None, "expected a SELL signal on PDH sweep+reclaim"
    assert out.side == "SELL"
    assert "PDH" in out.regime
    # Stop above entry, target below — and PDL should be the natural target.
    assert out.stop > out.entry_price
    assert out.target < out.entry_price


def test_sweep_pdl_produces_long_signal() -> None:
    s = AnchorSweepStrategy(_permissive_cfg())
    cfg = _config()
    pdh = 200.0
    pdl = 190.0
    hist = _seed_pdh_pdl(s, cfg, pdh=pdh, pdl=pdl)

    # Sweep bar: low=189 (below pdl=190), close=190.5 (back above).
    sweep_bar = _bar(
        datetime(2026, 1, 16, 10, 0),
        high=192.0,
        low=189.0,
        open_=191.0,
        close=190.5,
        volume=2000.0,
    )
    hist.append(sweep_bar)
    out = s.maybe_enter(sweep_bar, hist, 10_000.0, cfg)
    assert out is not None, "expected a BUY signal on PDL sweep+reclaim"
    assert out.side == "BUY"
    assert "PDL" in out.regime
    assert out.stop < out.entry_price
    assert out.target > out.entry_price


# ---------------------------------------------------------------------------
# Hygiene caps
# ---------------------------------------------------------------------------


def test_max_trades_per_day_cap_is_enforced() -> None:
    """Once the cap is hit, no more trades fire on the same ET date."""
    s = AnchorSweepStrategy(_permissive_cfg(max_trades_per_day=1))
    cfg = _config()
    pdh = 200.0
    pdl = 190.0
    hist = _seed_pdh_pdl(s, cfg, pdh=pdh, pdl=pdl)

    # Fire-able sweep #1 — PDH sweep
    sweep1 = _bar(
        datetime(2026, 1, 16, 10, 0),
        high=201.0,
        low=198.0,
        open_=199.0,
        close=199.5,
        volume=2000.0,
    )
    hist.append(sweep1)
    out1 = s.maybe_enter(sweep1, hist, 10_000.0, cfg)
    assert out1 is not None  # first trade fires

    # Fire-able sweep #2 — also PDL same day. Cap=1 should block it.
    sweep2 = _bar(
        datetime(2026, 1, 16, 14, 0),
        high=192.0,
        low=189.0,
        open_=191.0,
        close=190.5,
        volume=2000.0,
    )
    hist.append(sweep2)
    out2 = s.maybe_enter(sweep2, hist, 10_000.0, cfg)
    assert out2 is None, "second trade should be blocked by max_trades_per_day"


# ---------------------------------------------------------------------------
# Signal validator parity
# ---------------------------------------------------------------------------


def test_pdh_short_signal_passes_validator() -> None:
    s = AnchorSweepStrategy(_permissive_cfg())
    cfg = _config()
    hist = _seed_pdh_pdl(s, cfg, pdh=200.0, pdl=190.0)
    sweep_bar = _bar(
        datetime(2026, 1, 16, 10, 0),
        high=201.0,
        low=198.0,
        open_=199.0,
        close=199.5,
        volume=2000.0,
    )
    hist.append(sweep_bar)
    out = s.maybe_enter(sweep_bar, hist, 10_000.0, cfg)
    assert out is not None
    res = validate_signal(
        side=out.side,
        entry=out.entry_price,
        stop=out.stop,
        target=out.target,
        qty=out.qty,
        equity=10_000.0,
        point_value=2.0,  # MNQ
    )
    assert res.ok, res.failures


def test_pdl_long_signal_passes_validator() -> None:
    s = AnchorSweepStrategy(_permissive_cfg())
    cfg = _config()
    hist = _seed_pdh_pdl(s, cfg, pdh=200.0, pdl=190.0)
    sweep_bar = _bar(
        datetime(2026, 1, 16, 10, 0),
        high=192.0,
        low=189.0,
        open_=191.0,
        close=190.5,
        volume=2000.0,
    )
    hist.append(sweep_bar)
    out = s.maybe_enter(sweep_bar, hist, 10_000.0, cfg)
    assert out is not None
    res = validate_signal(
        side=out.side,
        entry=out.entry_price,
        stop=out.stop,
        target=out.target,
        qty=out.qty,
        equity=10_000.0,
        point_value=2.0,
    )
    assert res.ok, res.failures


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def test_mnq_and_nq_presets_construct() -> None:
    mnq = mnq_anchor_sweep_preset()
    nq = nq_anchor_sweep_preset()
    assert mnq.anchor_set == ("PDH", "PDL", "PMH", "PML", "ONH", "ONL")
    assert nq.anchor_set == mnq.anchor_set
    # Both are on the same Nasdaq-100 underlying — risk knobs match
    assert mnq.atr_stop_mult == nq.atr_stop_mult
    assert mnq.max_trades_per_day == nq.max_trades_per_day
    AnchorSweepStrategy(mnq)
    AnchorSweepStrategy(nq)
