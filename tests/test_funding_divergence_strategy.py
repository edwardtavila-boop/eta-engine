"""Tests for FundingDivergenceStrategy.

Built for the 2026-04-27 supercharge thread (commit 973a6aa) —
regime-invariant strategy that mean-reverts on positioning
extremes, NOT on price direction. Designed to provide edge
across all regimes (bull/bear/sideways) since the mechanic is
position unwind, not directional bet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.funding_divergence_strategy import (
    FundingDivergenceConfig,
    FundingDivergenceStrategy,
)


def _bar(idx: int, close: float = 100.0) -> BarData:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=idx)
    return BarData(
        timestamp=ts, symbol="BTC", open=close,
        high=close + 1.0, low=close - 1.0, close=close, volume=1000.0,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="BTC", initial_equity=10_000.0,
        risk_per_trade_pct=0.01, confluence_threshold=0.0,
        max_trades_per_day=10,
    )


# ---------------------------------------------------------------------------
# Basic mechanics
# ---------------------------------------------------------------------------


def test_returns_none_without_funding_provider() -> None:
    """No funding provider attached → strategy is inert."""
    s = FundingDivergenceStrategy(FundingDivergenceConfig(warmup_bars=1))
    cfg = _config()
    hist: list[BarData] = []
    for i in range(50):
        b = _bar(i)
        hist.append(b)
        assert s.maybe_enter(b, hist, 10_000.0, cfg) is None


def test_warmup_blocks_entries() -> None:
    """During warmup, no entries fire even with extreme funding."""
    s = FundingDivergenceStrategy(FundingDivergenceConfig(
        entry_threshold=0.0001, warmup_bars=20,
    ))
    s.attach_funding_provider(lambda b: 0.01)  # extreme overheated longs
    cfg = _config()
    hist: list[BarData] = []
    for i in range(10):
        b = _bar(i)
        hist.append(b)
        assert s.maybe_enter(b, hist, 10_000.0, cfg) is None


def test_long_when_funding_extreme_negative() -> None:
    """Funding < -threshold → BUY (mean-revert capitulated shorts)."""
    s = FundingDivergenceStrategy(FundingDivergenceConfig(
        entry_threshold=0.001,
        warmup_bars=20,
        atr_period=10,
        min_bars_between_trades=0,
    ))
    s.attach_funding_provider(lambda b: -0.01)  # extreme negative
    cfg = _config()
    hist: list[BarData] = []
    out = None
    for i in range(50):
        b = _bar(i)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None:
            break
    assert out is not None
    assert out.side == "BUY"
    assert "funding_div" in out.regime


def test_short_when_funding_extreme_positive() -> None:
    """Funding > +threshold → SELL (mean-revert overheated longs)."""
    s = FundingDivergenceStrategy(FundingDivergenceConfig(
        entry_threshold=0.001,
        warmup_bars=20,
        atr_period=10,
        min_bars_between_trades=0,
    ))
    s.attach_funding_provider(lambda b: 0.01)
    cfg = _config()
    hist: list[BarData] = []
    out = None
    for i in range(50):
        b = _bar(i)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None:
            break
    assert out is not None
    assert out.side == "SELL"


def test_no_entry_at_neutral_funding() -> None:
    """Funding within threshold → no entry."""
    s = FundingDivergenceStrategy(FundingDivergenceConfig(
        entry_threshold=0.001, warmup_bars=20, atr_period=10,
        min_bars_between_trades=0,
    ))
    s.attach_funding_provider(lambda b: 0.0001)  # below threshold
    cfg = _config()
    hist: list[BarData] = []
    for i in range(50):
        b = _bar(i)
        hist.append(b)
        assert s.maybe_enter(b, hist, 10_000.0, cfg) is None


def test_cooldown_throttles_consecutive_entries() -> None:
    """min_bars_between_trades blocks rapid-fire entries."""
    s = FundingDivergenceStrategy(FundingDivergenceConfig(
        entry_threshold=0.001, warmup_bars=20, atr_period=10,
        min_bars_between_trades=24,
        max_trades_per_day=999,  # disable per-day cap
    ))
    s.attach_funding_provider(lambda b: 0.01)
    cfg = _config()
    hist: list[BarData] = []
    fires = 0
    for i in range(50):
        b = _bar(i)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None:
            fires += 1
    # First fire at i=20, then min 24 bars cooldown → next at i=44.
    # 50 bars total, so we expect ≤ 2 fires.
    assert 1 <= fires <= 2


# ---------------------------------------------------------------------------
# Audit + stats
# ---------------------------------------------------------------------------


def test_stats_track_funding_extremes_and_fires() -> None:
    s = FundingDivergenceStrategy(FundingDivergenceConfig(
        entry_threshold=0.001, warmup_bars=10, atr_period=5,
        min_bars_between_trades=0,
    ))
    s.attach_funding_provider(lambda b: 0.01)
    cfg = _config()
    hist: list[BarData] = []
    for i in range(20):
        b = _bar(i)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    stats = s.stats
    assert stats["bars_seen"] == 20
    assert stats["funding_extreme_seen"] >= 1
    assert stats["entries_fired"] >= 1


# ---------------------------------------------------------------------------
# Optional directional confirmation
# ---------------------------------------------------------------------------


def test_directional_confirmation_vetoes_against_sage() -> None:
    """When require_directional_confirmation=True and sage disagrees,
    veto the funding-divergence trade."""
    from dataclasses import dataclass

    @dataclass
    class Verdict:
        direction: str
        conviction: float = 0.5

    s = FundingDivergenceStrategy(FundingDivergenceConfig(
        entry_threshold=0.001, warmup_bars=10, atr_period=5,
        min_bars_between_trades=0,
        require_directional_confirmation=True,
    ))
    s.attach_funding_provider(lambda b: 0.01)  # would short
    # But sage says LONG → veto the SHORT
    s.attach_daily_verdict_provider(lambda d: Verdict(direction="long"))
    cfg = _config()
    hist: list[BarData] = []
    fired = 0
    for i in range(30):
        b = _bar(i)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None:
            fired += 1
    assert fired == 0
    assert s.stats["directional_vetoes"] >= 1


def test_directional_confirmation_allows_aligned_sage() -> None:
    """When sage agrees with funding direction, trade fires."""
    from dataclasses import dataclass

    @dataclass
    class Verdict:
        direction: str
        conviction: float = 0.5

    s = FundingDivergenceStrategy(FundingDivergenceConfig(
        entry_threshold=0.001, warmup_bars=10, atr_period=5,
        min_bars_between_trades=0,
        require_directional_confirmation=True,
    ))
    s.attach_funding_provider(lambda b: 0.01)  # SHORT signal
    s.attach_daily_verdict_provider(lambda d: Verdict(direction="short"))
    cfg = _config()
    hist: list[BarData] = []
    out = None
    for i in range(30):
        b = _bar(i)
        hist.append(b)
        candidate = s.maybe_enter(b, hist, 10_000.0, cfg)
        if candidate is not None:
            out = candidate
            break
    assert out is not None
    assert out.side == "SELL"
