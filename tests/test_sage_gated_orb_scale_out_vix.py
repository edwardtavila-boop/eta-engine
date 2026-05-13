"""Tests for sage_gated_orb_strategy redesign: scale-out + VIX filter.

Covers the 2026-05-07 redesign:
  * Backtest engine honours ``_Open.partial_target`` (Option A scale-out).
  * ``SageGatedORBStrategy`` emits a ``partial_target`` when configured.
  * VIX-spike filter blocks entries above the rolling p90 threshold.
  * Audit counters (_n_vix_filtered, _n_partial_exits_emitted) track
    each gate firing across the strategy lifetime.
  * VIX-disabled / scale-out-disabled paths preserve legacy behaviour.

The sage call is monkey-patched (same pattern as test_sage_strategies.py)
so these tests stay deterministic and don't depend on the 22-school
consultation engine being warm.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from eta_engine.backtest.engine import BacktestEngine, _Open
from eta_engine.backtest.models import BacktestConfig
from eta_engine.brain.jarvis_v3.sage.base import Bias, SageReport, SchoolVerdict
from eta_engine.core.data_pipeline import BarData
from eta_engine.features.pipeline import FeaturePipeline
from eta_engine.strategies.orb_strategy import ORBConfig
from eta_engine.strategies.sage_consensus_strategy import SageConsensusConfig
from eta_engine.strategies.sage_gated_orb_strategy import (
    SageGatedORBConfig,
    SageGatedORBStrategy,
)

_NY = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Bar fixtures
# ---------------------------------------------------------------------------


def _bar(
    h: float = 110.0,
    low: float = 100.0,
    *,
    c: float | None = None,
    o: float | None = None,
    v: float = 1000.0,
    minute: int = 0,
) -> BarData:
    """Synthetic UTC bar at 2026-01-15 09:30 + minute."""
    base = datetime(2026, 1, 15, 9, 30, tzinfo=_NY)
    ts = (base + timedelta(minutes=minute)).astimezone(UTC)
    return BarData(
        timestamp=ts,
        symbol="MNQ",
        open=o if o is not None else (h + low) / 2,
        high=h,
        low=low,
        close=c if c is not None else (h + low) / 2,
        volume=v,
    )


def _backtest_config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 1, 31, tzinfo=UTC),
        symbol="MNQ",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )


# ---------------------------------------------------------------------------
# Engine-level scale-out tests (Option A: _Open.partial_target)
# ---------------------------------------------------------------------------


class _StaticEntryStrategy:
    """Tiny deterministic strategy that returns ONE _Open then None.

    Used to exercise the engine's partial-exit bookkeeping without
    pulling in ORB's full state machine. Emits a long _Open with
    a 1.5R partial and a 3.5R runner target, mimicking the live
    config from ``mnq_futures_sage``.
    """

    def __init__(self, opened: _Open | None) -> None:
        self._opened = opened
        self._fired = False

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        if self._fired:
            return None
        self._fired = True
        return self._opened


def _build_runner_open(
    *,
    side: str = "BUY",
    entry: float = 100.0,
    stop: float = 90.0,
    rr_partial: float = 1.5,
    rr_target: float = 3.5,
    partial_qty_frac: float = 0.5,
    qty: float = 1.0,
) -> _Open:
    """Build a 1.5R-partial + 3.5R-runner _Open in long or short orientation."""
    stop_dist = abs(entry - stop)
    if side == "BUY":
        partial = entry + rr_partial * stop_dist
        target = entry + rr_target * stop_dist
    else:
        partial = entry - rr_partial * stop_dist
        target = entry - rr_target * stop_dist
    base_bar = _bar(h=entry + 5, low=entry - 5, c=entry, minute=0)
    return _Open(
        entry_bar=base_bar,
        side=side,
        qty=qty,
        entry_price=entry,
        stop=stop,
        target=target,
        risk_usd=stop_dist * qty,
        confluence=10.0,
        leverage=1.0,
        partial_target=partial,
        partial_qty_frac=partial_qty_frac,
    )


def test_open_partial_target_invariants_long() -> None:
    """LONG: partial must lie between entry and target."""
    # Valid: partial between entry and target.
    _build_runner_open(side="BUY", entry=100.0, stop=90.0, rr_partial=1.5, rr_target=3.5)
    # Invalid: partial above target.
    with pytest.raises(ValueError, match="partial_target"):
        bar = _bar(h=105, low=95, c=100)
        _Open(
            entry_bar=bar,
            side="BUY",
            qty=1.0,
            entry_price=100.0,
            stop=90.0,
            target=105.0,
            risk_usd=10.0,
            confluence=0.0,
            leverage=1.0,
            partial_target=110.0,  # > target
            partial_qty_frac=0.5,
        )


def test_open_partial_target_invariants_short() -> None:
    """SHORT: partial must lie between target and entry."""
    _build_runner_open(side="SELL", entry=100.0, stop=110.0, rr_partial=1.5, rr_target=3.5)
    # Invalid: partial below target on SHORT.
    with pytest.raises(ValueError, match="partial_target"):
        bar = _bar(h=105, low=95, c=100)
        _Open(
            entry_bar=bar,
            side="SELL",
            qty=1.0,
            entry_price=100.0,
            stop=110.0,
            target=95.0,
            risk_usd=10.0,
            confluence=0.0,
            leverage=1.0,
            partial_target=90.0,  # < target on SHORT
            partial_qty_frac=0.5,
        )


def test_open_partial_qty_frac_must_be_in_open_unit_interval() -> None:
    bar = _bar(h=105, low=95, c=100)
    with pytest.raises(ValueError, match="partial_qty_frac"):
        _Open(
            entry_bar=bar,
            side="BUY",
            qty=1.0,
            entry_price=100.0,
            stop=90.0,
            target=120.0,
            risk_usd=10.0,
            confluence=0.0,
            leverage=1.0,
            partial_target=115.0,
            partial_qty_frac=1.5,  # out of range
        )


def test_engine_partial_exit_locks_in_cushion_and_runs_to_target() -> None:
    """Bar 1 prints through the partial; bar 2 prints through the runner target.

    Expected:
      * Engine fires the partial on bar 1 (locks in 0.5 × 1.5R cushion).
      * Engine then closes the runner on bar 2 at target (0.5 × 3.5R).
      * Total pnl_R ≈ 0.5*1.5 + 0.5*3.5 = 2.5R, exit_reason="runner_target_hit".
    """
    opened = _build_runner_open(
        side="BUY",
        entry=100.0,
        stop=90.0,
        rr_partial=1.5,
        rr_target=3.5,
        partial_qty_frac=0.5,
        qty=1.0,
    )
    # Bar 0 = entry bar (won't trigger partial — partial is at 115)
    # Bar 1: high=116 → partial hits; low must stay above BE (100) so
    # the runner isn't stopped on the same bar that took the partial.
    # Bar 2: high=140 → runner target hits
    bars = [
        _bar(h=105, low=98, c=100, minute=0),
        _bar(h=116, low=110, c=115.5, minute=5),
        _bar(h=140, low=115, c=138, minute=10),
    ]
    strategy = _StaticEntryStrategy(opened)
    cfg = _backtest_config()
    engine = BacktestEngine(
        pipeline=FeaturePipeline.default(),
        config=cfg,
        strategy=strategy,
    )
    res = engine.run(bars)
    assert len(res.trades) == 1
    t = res.trades[0]
    # Partial 0.5 × +1.5R cushion + runner 0.5 × +3.5R = +2.5R total
    assert t.pnl_r == pytest.approx(2.5, abs=0.01)
    assert t.exit_reason == "runner_target_hit"


def test_engine_partial_exit_then_runner_stops_out_at_be() -> None:
    """Partial fires, stop moves to entry; runner then taps entry → BE stop.

    Expected pnl: +0.5 × 1.5R cushion + 0.5 × 0R = +0.75R.
    """
    opened = _build_runner_open(
        side="BUY",
        entry=100.0,
        stop=90.0,
        rr_partial=1.5,
        rr_target=3.5,
        partial_qty_frac=0.5,
        qty=1.0,
    )
    # Bar 1: partial hits at 115
    # Bar 2: low touches 100 (entry) → runner BE stop fires
    bars = [
        _bar(h=105, low=98, c=100, minute=0),
        _bar(h=116, low=110, c=115.5, minute=5),
        _bar(h=120, low=99.5, c=99.6, minute=10),
    ]
    strategy = _StaticEntryStrategy(opened)
    cfg = _backtest_config()
    engine = BacktestEngine(
        pipeline=FeaturePipeline.default(),
        config=cfg,
        strategy=strategy,
    )
    res = engine.run(bars)
    assert len(res.trades) == 1
    t = res.trades[0]
    # 0.5 × 1.5R + 0.5 × 0R = 0.75R
    assert t.pnl_r == pytest.approx(0.75, abs=0.01)
    assert t.exit_reason == "runner_stop_hit"


def test_engine_partial_short_side_pnl_correct() -> None:
    """SHORT scale-out: partial at -1.5R, runner to -3.5R, BE stop on retrace."""
    opened = _build_runner_open(
        side="SELL",
        entry=100.0,
        stop=110.0,
        rr_partial=1.5,
        rr_target=3.5,
        partial_qty_frac=0.5,
        qty=1.0,
    )
    # Partial at 100 - 15 = 85; target at 100 - 35 = 65
    bars = [
        _bar(h=102, low=95, c=100, minute=0),
        _bar(h=99, low=84, c=84.5, minute=5),  # partial hits (low <= 85)
        _bar(h=99, low=64, c=66, minute=10),  # runner target (low <= 65)
    ]
    strategy = _StaticEntryStrategy(opened)
    cfg = _backtest_config()
    engine = BacktestEngine(
        pipeline=FeaturePipeline.default(),
        config=cfg,
        strategy=strategy,
    )
    res = engine.run(bars)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.pnl_r == pytest.approx(2.5, abs=0.01)
    assert t.exit_reason == "runner_target_hit"


def test_engine_legacy_no_partial_target_unchanged() -> None:
    """When partial_target is None, engine behaves identically to legacy."""
    bar = _bar(h=105, low=95, c=100, minute=0)
    opened = _Open(
        entry_bar=bar,
        side="BUY",
        qty=1.0,
        entry_price=100.0,
        stop=90.0,
        target=120.0,
        risk_usd=10.0,
        confluence=10.0,
        leverage=1.0,
        partial_target=None,
    )
    bars = [
        _bar(h=105, low=98, c=100, minute=0),
        _bar(h=121, low=110, c=120, minute=5),
    ]
    strategy = _StaticEntryStrategy(opened)
    cfg = _backtest_config()
    engine = BacktestEngine(
        pipeline=FeaturePipeline.default(),
        config=cfg,
        strategy=strategy,
    )
    res = engine.run(bars)
    assert len(res.trades) == 1
    t = res.trades[0]
    # Full target hit, no partial taken → +2R, exit_reason="target_hit"
    assert t.pnl_r == pytest.approx(2.0, abs=0.01)
    assert t.exit_reason == "target_hit"


# ---------------------------------------------------------------------------
# SageGatedORB scale-out emission tests
# ---------------------------------------------------------------------------


def _fake_report(
    bias: Bias,
    conviction: float,
    alignment: float = 1.0,
    n_aligned: int = 15,
    n_disagree: int = 2,
    n_neutral: int = 5,
) -> SageReport:
    v = SchoolVerdict(
        school="fake",
        bias=bias,
        conviction=conviction,
        aligned_with_entry=(bias != Bias.NEUTRAL),
        rationale="test fixture",
    )
    return SageReport(
        per_school={"fake": v},
        composite_bias=bias,
        conviction=conviction,
        schools_consulted=n_aligned + n_disagree + n_neutral,
        schools_aligned_with_entry=n_aligned,
        schools_disagreeing_with_entry=n_disagree,
        schools_neutral=n_neutral,
        rationale="test",
    )


@pytest.fixture
def fake_sage(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Install a controllable sage stub (mirrors test_sage_strategies)."""

    class _FakeSage:
        next_report: SageReport | None = None
        call_count: int = 0
        last_ctx: Any = None
        raise_next: bool = False

        def __call__(self, ctx: Any, **kwargs: Any) -> SageReport:
            self.call_count += 1
            self.last_ctx = ctx
            if self.raise_next:
                self.raise_next = False
                raise RuntimeError("simulated sage failure")
            if self.next_report is None:
                return _fake_report(Bias.NEUTRAL, 0.0, 0.0)
            return self.next_report

    fake = _FakeSage()
    monkeypatch.setattr(
        "eta_engine.brain.jarvis_v3.sage.consultation.consult_sage",
        fake,
    )
    return fake


def _ny_bar(
    local_h: int,
    local_m: int,
    *,
    high: float,
    low: float,
    close: float | None = None,
    open_: float | None = None,
    volume: float = 1000.0,
    day: int = 15,
) -> BarData:
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


def _gated_orb(
    *,
    overlay_enabled: bool = True,
    enable_scale_out: bool = True,
    enable_vix_filter: bool = False,  # default OFF in tests; opt in per-test
    rr_partial: float = 1.5,
    partial_qty_frac: float = 0.5,
    rr_target: float = 3.5,
    vix_provider: Any = None,  # type: ignore[arg-type]
    vix_lookback_bars: int = 5,
    vix_pct_threshold: float = 0.90,
    **sage_overrides: Any,
) -> SageGatedORBStrategy:
    sage_base = {"min_conviction": 0.5, "min_alignment": 0.5}
    sage_base.update(sage_overrides)
    cfg = SageGatedORBConfig(
        orb=ORBConfig(
            ema_bias_period=0,
            volume_mult=0.0,
            atr_period=5,
            range_minutes=15,
            require_retest=False,
            rr_target=rr_target,
            atr_stop_mult=2.0,
        ),
        sage=SageConsensusConfig(**sage_base),
        overlay_enabled=overlay_enabled,
        enable_scale_out=enable_scale_out,
        rr_partial=rr_partial,
        partial_qty_frac=partial_qty_frac,
        enable_vix_filter=enable_vix_filter,
        vix_lookback_bars=vix_lookback_bars,
        vix_pct_threshold=vix_pct_threshold,
    )
    return SageGatedORBStrategy(cfg, vix_provider=vix_provider)


def _drive_orb_range(s: SageGatedORBStrategy, *, hi: float, lo: float) -> list[BarData]:
    cfg = _backtest_config()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_ny_bar(h, m, high=hi, low=lo), [], 10_000.0, cfg)
    hist: list[BarData] = []
    for h in (4, 5, 6, 7, 8):
        for m in range(0, 60, 10):
            hist.append(_ny_bar(h, m, high=hi, low=lo))
    return hist


def test_gated_orb_emits_partial_target_on_fire(fake_sage) -> None:
    """When sage agrees and scale-out is on, the _Open carries partial_target."""
    s = _gated_orb(enable_scale_out=True, rr_partial=1.5, rr_target=3.5)
    cfg = _backtest_config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    fake_sage.next_report = _fake_report(
        Bias.LONG,
        conviction=0.9,
        alignment=1.0,
    )
    bar = _ny_bar(9, 45, high=125, low=120, close=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.partial_target is not None
    # partial_target should be at entry + 1.5 × stop_dist
    stop_dist = abs(out.entry_price - out.stop)
    expected = out.entry_price + 1.5 * stop_dist
    assert out.partial_target == pytest.approx(expected, abs=0.01)
    # And the runner target is the ORB's rr_target × stop_dist away
    assert out.target == pytest.approx(out.entry_price + 3.5 * stop_dist, abs=0.01)
    assert out.partial_qty_frac == 0.5
    # Audit counter incremented
    assert s.n_partial_exits_emitted == 1


def test_gated_orb_no_partial_when_scale_out_disabled(fake_sage) -> None:
    """enable_scale_out=False → legacy single-target _Open (no partial_target)."""
    s = _gated_orb(enable_scale_out=False, rr_target=3.5)
    cfg = _backtest_config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    fake_sage.next_report = _fake_report(Bias.LONG, conviction=0.9, alignment=1.0)
    bar = _ny_bar(9, 45, high=125, low=120, close=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.partial_target is None
    assert s.n_partial_exits_emitted == 0


def test_gated_orb_passthrough_with_scale_out_emits_partial(fake_sage) -> None:
    """overlay_enabled=False but enable_scale_out=True still emits partial."""
    s = _gated_orb(overlay_enabled=False, enable_scale_out=True)
    cfg = _backtest_config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    fake_sage.next_report = _fake_report(Bias.SHORT, 0.99, 0.0)  # would veto
    bar = _ny_bar(9, 45, high=125, low=120, close=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert out.partial_target is not None
    # Sage shouldn't have been called when overlay is off.
    assert fake_sage.call_count == 0


# ---------------------------------------------------------------------------
# VIX filter tests
# ---------------------------------------------------------------------------


def _build_vix_provider(values_by_minute: dict[int, float]):  # type: ignore[no-untyped-def]
    """Build a callable VIX provider from {minute_offset: vix_close}.

    The provider matches a bar by the minute offset within the day,
    so tests can deliver varying VIX values over a sequence of bars.
    """

    def _provider(bar: BarData) -> float | None:
        local = bar.timestamp.astimezone(_NY)
        key = local.hour * 60 + local.minute
        return values_by_minute.get(key)

    return _provider


def test_vix_filter_blocks_entry_above_p90(fake_sage) -> None:
    """Once VIX history is warm and current VIX > p90, entry is blocked."""
    # Build a VIX history of 5 bars with values [10, 11, 12, 13, 14],
    # then a current bar at VIX=20 (well above p90).
    minute_to_vix = {
        # warmup bars (during the range build, which calls maybe_enter)
        # plus one current bar; we'll feed 5 unique-timestamped bars
        # before the breakout fires.
        0: 10.0,  # 9:30
        5: 11.0,  # 9:35
        10: 12.0,  # 9:40
        # bars 9:45 ... will be the breakout-firing minute. We seed
        # warm history via direct calls below.
    }
    provider = _build_vix_provider(minute_to_vix)
    s = _gated_orb(
        enable_vix_filter=True,
        vix_lookback_bars=5,
        vix_pct_threshold=0.90,
        vix_provider=provider,
    )
    # Manually warm the VIX buffer to just over the lookback window
    # so the percentile computation is well-defined.
    s._vix_history = [10.0, 11.0, 12.0, 13.0, 14.0]
    s._last_vix_ts = None  # force append on next call

    cfg = _backtest_config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    fake_sage.next_report = _fake_report(Bias.LONG, 0.9, 1.0)

    # Make 9:45 a VIX spike (current = 20).
    minute_to_vix[15 * 60 + 45 - 9 * 60] = 20.0  # safety; covered by mapping below
    minute_to_vix[(9 * 60 + 45)] = 20.0  # 9:45 NY -> minute = 9*60+45=585; map keys are h*60+m
    # Above we used h*60+m as key, so 9:45 = 585
    bar = _ny_bar(9, 45, high=125, low=120, close=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is None, "VIX above p90 should block entry"
    assert s.n_vix_filtered == 1


def test_vix_filter_allows_entry_below_p90(fake_sage) -> None:
    """When current VIX is below the rolling p90, entry proceeds."""
    s = _gated_orb(
        enable_vix_filter=True,
        vix_lookback_bars=5,
        vix_pct_threshold=0.90,
        vix_provider=_build_vix_provider({(9 * 60 + 45): 10.0}),
    )
    s._vix_history = [10.0, 11.0, 12.0, 13.0, 14.0]
    s._last_vix_ts = None

    cfg = _backtest_config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    fake_sage.next_report = _fake_report(Bias.LONG, 0.9, 1.0)
    bar = _ny_bar(9, 45, high=125, low=120, close=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None, "VIX below p90 should allow entry"
    assert s.n_vix_filtered == 0


def test_vix_filter_disabled_preserves_legacy_behavior(fake_sage) -> None:
    """enable_vix_filter=False → strategy never calls VIX provider."""
    calls: list[BarData] = []

    def _spy(bar: BarData) -> float | None:
        calls.append(bar)
        return 999.0  # would block if filter were on

    s = _gated_orb(
        enable_vix_filter=False,
        vix_provider=_spy,
    )
    cfg = _backtest_config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    fake_sage.next_report = _fake_report(Bias.LONG, 0.9, 1.0)
    bar = _ny_bar(9, 45, high=125, low=120, close=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert calls == [], "VIX provider must not be called when filter disabled"


def test_vix_filter_warmup_fail_open(fake_sage) -> None:
    """During buffer warmup (< vix_lookback_bars samples), filter is no-op."""
    s = _gated_orb(
        enable_vix_filter=True,
        vix_lookback_bars=100,  # never warm in this short test
        vix_pct_threshold=0.90,
        vix_provider=_build_vix_provider({(9 * 60 + 45): 100.0}),
    )
    cfg = _backtest_config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    fake_sage.next_report = _fake_report(Bias.LONG, 0.9, 1.0)
    bar = _ny_bar(9, 45, high=125, low=120, close=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None, "warmup must fail-open"
    assert s.n_vix_filtered == 0


def test_vix_filter_resets_orb_day_state_on_block(fake_sage) -> None:
    """A VIX-blocked entry must not lock out the rest of the day."""
    minute_to_vix: dict[int, float] = {}

    def _provider(bar: BarData) -> float | None:
        local = bar.timestamp.astimezone(_NY)
        key = local.hour * 60 + local.minute
        return minute_to_vix.get(key)

    s = _gated_orb(
        enable_vix_filter=True,
        vix_lookback_bars=5,
        vix_pct_threshold=0.90,
        vix_provider=_provider,
    )
    s._vix_history = [10.0, 11.0, 12.0, 13.0, 14.0]
    s._last_vix_ts = None

    cfg = _backtest_config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    fake_sage.next_report = _fake_report(Bias.LONG, 0.9, 1.0)
    # First fire: VIX spike → block.
    minute_to_vix[(9 * 60 + 45)] = 50.0
    bar1 = _ny_bar(9, 45, high=125, low=120, close=124)
    out1 = s.maybe_enter(bar1, hist, 10_000.0, cfg)
    assert out1 is None
    # Second fire: VIX low → trade should fire.
    minute_to_vix[(10 * 60)] = 10.0
    bar2 = _ny_bar(10, 0, high=126, low=121, close=125)
    out2 = s.maybe_enter(bar2, hist, 10_000.0, cfg)
    assert out2 is not None, "ORB day-state must reset on VIX veto"
    assert out2.side == "BUY"
