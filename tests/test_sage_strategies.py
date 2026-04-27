"""Tests for sage-driven strategies — consensus + gated-ORB.

Sage runs 22 schools per bar, so the test suite mocks
``consult_sage`` to return canned SageReports. This keeps the test
deterministic + fast while still exercising the strategy plumbing
(thresholds, fail-open vs fail-closed, ORB-day-state rollback).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from eta_engine.backtest.models import BacktestConfig
from eta_engine.brain.jarvis_v3.sage.base import Bias, SageReport, SchoolVerdict
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.orb_strategy import ORBConfig
from eta_engine.strategies.sage_consensus_strategy import (
    SageConsensusConfig,
    SageConsensusStrategy,
)
from eta_engine.strategies.sage_gated_orb_strategy import (
    SageGatedORBConfig,
    SageGatedORBStrategy,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


_NY = ZoneInfo("America/New_York")


def _bar(idx: int, *, h: float, low: float, c: float | None = None,
         o: float | None = None, v: float = 1000.0,
         tf_minutes: int = 5, day: int = 15) -> BarData:
    """Synthetic bar at 2026-01-DD 09:30 NY + idx*tf_minutes."""
    base = datetime(2026, 1, day, 9, 30, tzinfo=_NY)
    ts = (base + timedelta(minutes=idx * tf_minutes)).astimezone(UTC)
    o = o if o is not None else (h + low) / 2
    c = c if c is not None else (h + low) / 2
    return BarData(
        timestamp=ts, symbol="MNQ", open=o, high=h, low=low, close=c, volume=v,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 1, 31, tzinfo=UTC),
        symbol="MNQ", initial_equity=10_000.0,
        risk_per_trade_pct=0.01, confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _fake_report(
    bias: Bias, conviction: float, alignment: float, n_aligned: int = 12,
    n_disagree: int = 4, n_neutral: int = 6,
) -> SageReport:
    """Build a SageReport with the given composite + counters.

    The per_school dict is empty — the strategy only reads the
    aggregated fields, so we don't need to mock individual schools.
    """
    # Need at least one verdict for consensus_pct to land non-zero;
    # synthesize one matching the composite so consensus_pct = 1.0.
    v = SchoolVerdict(
        school="fake", bias=bias, conviction=conviction,
        aligned_with_entry=(bias != Bias.NEUTRAL),
        rationale="test fixture",
    )
    # Build the report directly. alignment_score is a property derived
    # from aligned/disagree counters, so set them to produce the
    # intended ratio.
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


def _sage_module_paths() -> tuple[str, str]:
    """The dotted paths consult_sage is bound to inside each strategy.

    Both modules import the symbol locally inside maybe_enter, so
    monkeypatching the SOURCE module is the cleanest patch point.
    """
    return (
        "eta_engine.brain.jarvis_v3.sage.consultation.consult_sage",
        "eta_engine.brain.jarvis_v3.sage.consultation.consult_sage",
    )


@pytest.fixture
def fake_sage(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Install a controllable sage stub. Tests assign .next_report."""
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
                # Default = NEUTRAL, no signal.
                return _fake_report(Bias.NEUTRAL, 0.0, 0.0)
            return self.next_report

    fake = _FakeSage()
    src, _ = _sage_module_paths()
    monkeypatch.setattr(src, fake)
    return fake


# ---------------------------------------------------------------------------
# SageConsensusStrategy
# ---------------------------------------------------------------------------


def _consensus(**overrides) -> SageConsensusStrategy:  # type: ignore[no-untyped-def]
    base = {
        "warmup_bars": 5, "min_bars_between_trades": 0,
        "atr_period": 5, "min_conviction": 0.5, "min_consensus": 0.0,
        "min_alignment": 0.5,
    }
    base.update(overrides)
    return SageConsensusStrategy(SageConsensusConfig(**base))


def _drive_warmup(s: SageConsensusStrategy, n: int = 30) -> list[BarData]:
    """Push n warmup bars through so sage's regime detector can run."""
    cfg = _config()
    hist: list[BarData] = []
    for i in range(n):
        b = _bar(i, h=100 + i * 0.1, low=99 + i * 0.1, c=99.5 + i * 0.1)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    return hist


def test_consensus_warmup_blocks_early_signal(fake_sage) -> None:
    """During warmup, no trade fires regardless of sage output."""
    fake_sage.next_report = _fake_report(Bias.LONG, 0.99, 1.0)
    s = _consensus(warmup_bars=10)
    cfg = _config()
    for i in range(5):
        b = _bar(i, h=110, low=100, c=105)
        out = s.maybe_enter(b, [b], 10_000.0, cfg)
        assert out is None


def test_consensus_long_fires_on_strong_long_report(fake_sage) -> None:
    s = _consensus()
    hist = _drive_warmup(s, n=30)
    fake_sage.next_report = _fake_report(
        Bias.LONG, conviction=0.80, alignment=1.0,
        n_aligned=15, n_disagree=2, n_neutral=5,
    )
    bar = _bar(31, h=110, low=100, c=108)
    hist.append(bar)
    out = s.maybe_enter(bar, hist, 10_000.0, _config())
    assert out is not None
    assert out.side == "BUY"
    assert out.regime == "sage_long"


def test_consensus_short_fires_on_strong_short_report(fake_sage) -> None:
    s = _consensus()
    hist = _drive_warmup(s, n=30)
    # SHORT bias: aligned counters use ctx.side="long", so for SHORT
    # we need n_disagree > n_aligned (alignment_score < min_alignment
    # FOR LONG, but the strategy converts: real_alignment = 1 - score).
    fake_sage.next_report = _fake_report(
        Bias.SHORT, conviction=0.80, alignment=0.0,
        n_aligned=2, n_disagree=15, n_neutral=5,
    )
    bar = _bar(31, h=110, low=100, c=102)
    hist.append(bar)
    out = s.maybe_enter(bar, hist, 10_000.0, _config())
    assert out is not None
    assert out.side == "SELL"
    assert out.regime == "sage_short"


def test_consensus_blocks_on_low_conviction(fake_sage) -> None:
    s = _consensus(min_conviction=0.7)
    hist = _drive_warmup(s, n=30)
    fake_sage.next_report = _fake_report(
        Bias.LONG, conviction=0.4, alignment=1.0,
    )
    bar = _bar(31, h=110, low=100, c=108)
    hist.append(bar)
    assert s.maybe_enter(bar, hist, 10_000.0, _config()) is None


def test_consensus_blocks_on_neutral_bias(fake_sage) -> None:
    s = _consensus()
    hist = _drive_warmup(s, n=30)
    fake_sage.next_report = _fake_report(Bias.NEUTRAL, 0.99, 0.5)
    bar = _bar(31, h=110, low=100, c=108)
    hist.append(bar)
    assert s.maybe_enter(bar, hist, 10_000.0, _config()) is None


def test_consensus_blocks_when_alignment_too_low(fake_sage) -> None:
    """Strong LONG bias but only 51% of non-neutral schools agree."""
    s = _consensus(min_alignment=0.8)
    hist = _drive_warmup(s, n=30)
    fake_sage.next_report = _fake_report(
        Bias.LONG, conviction=0.99, alignment=0.51,
        n_aligned=10, n_disagree=9, n_neutral=3,
    )
    bar = _bar(31, h=110, low=100, c=108)
    hist.append(bar)
    assert s.maybe_enter(bar, hist, 10_000.0, _config()) is None


def test_consensus_safe_when_sage_raises(fake_sage) -> None:
    """A sage exception must not crash the strategy — fail-closed."""
    s = _consensus()
    hist = _drive_warmup(s, n=30)
    fake_sage.raise_next = True
    bar = _bar(31, h=110, low=100, c=108)
    hist.append(bar)
    assert s.maybe_enter(bar, hist, 10_000.0, _config()) is None


def test_consensus_min_bars_between_trades_latch(fake_sage) -> None:
    s = _consensus(min_bars_between_trades=5)
    hist = _drive_warmup(s, n=30)
    fake_sage.next_report = _fake_report(
        Bias.LONG, conviction=0.99, alignment=1.0,
        n_aligned=15, n_disagree=2, n_neutral=5,
    )
    bar1 = _bar(31, h=110, low=100, c=108)
    hist.append(bar1)
    out1 = s.maybe_enter(bar1, hist, 10_000.0, _config())
    assert out1 is not None
    bar2 = _bar(32, h=111, low=101, c=109)
    hist.append(bar2)
    out2 = s.maybe_enter(bar2, hist, 10_000.0, _config())
    assert out2 is None  # cooldown


# ---------------------------------------------------------------------------
# SageGatedORBStrategy
# ---------------------------------------------------------------------------


def _gated_orb(
    *, overlay_enabled: bool = True, **sage_overrides,  # type: ignore[no-untyped-def]
) -> SageGatedORBStrategy:
    sage_base = {"min_conviction": 0.5, "min_alignment": 0.5}
    sage_base.update(sage_overrides)
    cfg = SageGatedORBConfig(
        orb=ORBConfig(
            ema_bias_period=0, volume_mult=0.0, atr_period=5,
            range_minutes=15,
        ),
        sage=SageConsensusConfig(**sage_base),
        overlay_enabled=overlay_enabled,
    )
    return SageGatedORBStrategy(cfg)


def _ny_bar(local_h: int, local_m: int, *, high: float, low: float,
            close: float | None = None, open_: float | None = None,
            volume: float = 1000.0, day: int = 15) -> BarData:
    """ORB bar at NY local time."""
    local_dt = datetime(2026, 1, day, local_h, local_m, tzinfo=_NY)
    utc_dt = local_dt.astimezone(UTC)
    o = open_ if open_ is not None else (high + low) / 2
    c = close if close is not None else (high + low) / 2
    return BarData(
        timestamp=utc_dt, symbol="MNQ", open=o, high=high, low=low,
        close=c, volume=volume,
    )


def _drive_orb_range(
    s: SageGatedORBStrategy, *, hi: float, lo: float, day: int = 15,
) -> list[BarData]:
    """Run the 9:30/9:35/9:40 range bars through, return ATR-rich hist.

    Returns at least 30 prior bars so sage's regime detector (needs
    25+ bars to fire) won't short-circuit the overlay into pass-
    through mode. Missing this gives a misleading test signal.
    """
    cfg = _config()
    for h, m in [(9, 30), (9, 35), (9, 40)]:
        s.maybe_enter(_ny_bar(h, m, high=hi, low=lo, day=day), [], 10_000.0, cfg)
    # Build a 30-bar pre-RTH stream over the prior session at 5m
    # cadence; enough for sage to detect a regime.
    hist: list[BarData] = []
    for h in (4, 5, 6, 7, 8):
        for m in range(0, 60, 10):
            hist.append(_ny_bar(h, m, high=hi, low=lo, day=day))
    return hist


def test_gated_orb_passthrough_when_overlay_disabled(fake_sage) -> None:
    """overlay_enabled=False → behave identically to plain ORB."""
    s = _gated_orb(overlay_enabled=False)
    cfg = _config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    fake_sage.next_report = _fake_report(Bias.SHORT, 0.99, 0.0)  # would veto
    bar = _ny_bar(9, 45, high=125, low=120, close=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    # Sage should NOT have been called when overlay is off.
    assert fake_sage.call_count == 0


def test_gated_orb_blocks_when_sage_disagrees(fake_sage) -> None:
    s = _gated_orb()
    cfg = _config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    fake_sage.next_report = _fake_report(
        Bias.SHORT, conviction=0.9, alignment=0.0,
        n_aligned=2, n_disagree=15, n_neutral=5,
    )
    bar = _ny_bar(9, 45, high=125, low=120, close=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is None


def test_gated_orb_passes_when_sage_agrees(fake_sage) -> None:
    s = _gated_orb()
    cfg = _config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    fake_sage.next_report = _fake_report(
        Bias.LONG, conviction=0.9, alignment=1.0,
        n_aligned=15, n_disagree=2, n_neutral=5,
    )
    bar = _ny_bar(9, 45, high=125, low=120, close=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert out.regime == "orb_sage_confirmed"


def test_gated_orb_failopen_when_sage_raises(fake_sage) -> None:
    """A sage exception should NOT block ORB — overlay fails open."""
    s = _gated_orb()
    cfg = _config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    fake_sage.raise_next = True
    bar = _ny_bar(9, 45, high=125, low=120, close=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert out.regime == "orb_breakout"  # plain ORB regime — overlay short-circuited


def test_gated_orb_resets_day_state_on_veto(fake_sage) -> None:
    """A vetoed breakout must NOT lock out the rest of the day."""
    s = _gated_orb()
    cfg = _config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    # First fire: sage vetoes
    fake_sage.next_report = _fake_report(
        Bias.SHORT, conviction=0.9, alignment=0.0,
        n_aligned=2, n_disagree=15, n_neutral=5,
    )
    bar1 = _ny_bar(9, 45, high=125, low=120, close=124)
    out1 = s.maybe_enter(bar1, hist, 10_000.0, cfg)
    assert out1 is None
    # Second fire (later in the day): sage now agrees → should fire
    fake_sage.next_report = _fake_report(
        Bias.LONG, conviction=0.9, alignment=1.0,
        n_aligned=15, n_disagree=2, n_neutral=5,
    )
    bar2 = _ny_bar(10, 0, high=126, low=121, close=125)
    out2 = s.maybe_enter(bar2, hist, 10_000.0, cfg)
    assert out2 is not None, "ORB day-state must reset on overlay veto"
    assert out2.side == "BUY"


def test_gated_orb_blocks_when_conviction_too_low(fake_sage) -> None:
    s = _gated_orb(min_conviction=0.8)
    cfg = _config()
    hist = _drive_orb_range(s, hi=120, lo=100)
    fake_sage.next_report = _fake_report(
        Bias.LONG, conviction=0.4, alignment=1.0,
        n_aligned=15, n_disagree=2, n_neutral=5,
    )
    bar = _ny_bar(9, 45, high=125, low=120, close=124)
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None
