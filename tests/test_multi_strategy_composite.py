"""Tests for MultiStrategyComposite — one bot runs N strategies in parallel.

Built for the 2026-04-27 user mandate: "one bot can run multiple
strategies". Validates conflict resolution, callback routing,
audit, and that each sub's state advances every bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eta_engine.backtest.engine import _Open
from eta_engine.backtest.models import BacktestConfig, Trade
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.multi_strategy_composite import (
    MultiStrategyComposite,
    MultiStrategyConfig,
)


def _bar(idx: int, close: float = 100.0) -> BarData:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=idx)
    return BarData(
        timestamp=ts,
        symbol="BTC",
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=1000.0,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="BTC",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=0.0,
        max_trades_per_day=100,
    )


def _open(side: str, conf: float, entry: float, stop_dist: float, target_dist: float, regime: str = "stub") -> _Open:
    bar = _bar(0, entry)
    return _Open(
        entry_bar=bar,
        side=side,
        qty=1.0,
        entry_price=entry,
        stop=entry - stop_dist if side == "BUY" else entry + stop_dist,
        target=entry + target_dist if side == "BUY" else entry - target_dist,
        risk_usd=10.0,
        confluence=conf,
        leverage=1.0,
        regime=regime,
    )


@dataclass
class _AlwaysFireStub:
    side: str
    confluence: float
    fired: int = 0
    closed: int = 0
    target_dist: float = 3.0
    stop_dist: float = 1.0

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        self.fired += 1
        return _open(self.side, self.confluence, bar.close, self.stop_dist, self.target_dist)

    def on_trade_close(self, trade: Trade) -> None:
        self.closed += 1


@dataclass
class _NeverFireStub:
    fired: int = 0

    def maybe_enter(
        self,
        *args,
        **kwargs,
    ) -> _Open | None:
        self.fired += 1
        return None


# ---------------------------------------------------------------------------
# Basic mechanics
# ---------------------------------------------------------------------------


def test_empty_subs_raises() -> None:
    """At least one sub-strategy required."""
    import pytest

    with pytest.raises(ValueError, match="at least 1 sub"):
        MultiStrategyComposite([])


def test_all_subs_state_advances_every_bar() -> None:
    """Even when one sub wins, ALL subs get maybe_enter() called."""
    s1 = _AlwaysFireStub("BUY", 5.0)
    s2 = _NeverFireStub()
    s3 = _NeverFireStub()
    composite = MultiStrategyComposite(
        [("s1", s1), ("s2", s2), ("s3", s3)],
    )
    cfg = _config()
    for i in range(10):
        composite.maybe_enter(_bar(i), [_bar(i)], 10_000.0, cfg)
    # Every sub fired its maybe_enter on every bar
    assert s1.fired == 10
    assert s2.fired == 10
    assert s3.fired == 10


def test_priority_policy_first_wins() -> None:
    """Default priority policy: first sub in list wins on conflicts."""
    s1 = _AlwaysFireStub("BUY", 1.0)  # lower confluence
    s2 = _AlwaysFireStub("BUY", 9.0)  # higher confluence but later
    composite = MultiStrategyComposite(
        [("s1", s1), ("s2", s2)],
        MultiStrategyConfig(conflict_policy="priority"),
    )
    out = composite.maybe_enter(_bar(0), [_bar(0)], 10_000.0, _config())
    assert out is not None
    assert composite.fire_records["s1"]["selected"] == 1
    assert composite.fire_records["s2"]["selected"] == 0


def test_confluence_weighted_picks_highest() -> None:
    s1 = _AlwaysFireStub("BUY", 1.0)
    s2 = _AlwaysFireStub("BUY", 9.0)
    composite = MultiStrategyComposite(
        [("s1", s1), ("s2", s2)],
        MultiStrategyConfig(conflict_policy="confluence_weighted"),
    )
    out = composite.maybe_enter(_bar(0), [_bar(0)], 10_000.0, _config())
    assert out is not None
    assert composite.fire_records["s1"]["selected"] == 0
    assert composite.fire_records["s2"]["selected"] == 1


def test_best_rr_picks_highest_rr() -> None:
    s1 = _AlwaysFireStub("BUY", 5.0, target_dist=2.0, stop_dist=1.0)  # 2.0 R-R
    s2 = _AlwaysFireStub("BUY", 5.0, target_dist=5.0, stop_dist=1.0)  # 5.0 R-R
    composite = MultiStrategyComposite(
        [("s1", s1), ("s2", s2)],
        MultiStrategyConfig(conflict_policy="best_rr"),
    )
    composite.maybe_enter(_bar(0), [_bar(0)], 10_000.0, _config())
    assert composite.fire_records["s2"]["selected"] == 1


def test_no_winner_when_no_subs_fire() -> None:
    s1 = _NeverFireStub()
    s2 = _NeverFireStub()
    composite = MultiStrategyComposite([("s1", s1), ("s2", s2)])
    out = composite.maybe_enter(_bar(0), [_bar(0)], 10_000.0, _config())
    assert out is None


# ---------------------------------------------------------------------------
# Audit + tagging
# ---------------------------------------------------------------------------


def test_originator_tagged_on_trade() -> None:
    """When tag_originator=True, regime field carries the sub name."""
    s1 = _AlwaysFireStub("BUY", 5.0)
    composite = MultiStrategyComposite([("alpha", s1)])
    out = composite.maybe_enter(_bar(0), [_bar(0)], 10_000.0, _config())
    assert out is not None
    assert "origin_alpha" in out.regime


def test_fire_records_track_per_sub_counts() -> None:
    s1 = _AlwaysFireStub("BUY", 5.0)
    s2 = _AlwaysFireStub("BUY", 1.0)
    composite = MultiStrategyComposite(
        [("s1", s1), ("s2", s2)],
        MultiStrategyConfig(conflict_policy="priority"),
    )
    cfg = _config()
    for i in range(5):
        composite.maybe_enter(_bar(i), [_bar(i)], 10_000.0, cfg)
    rec = composite.fire_records
    # Both fired every bar
    assert rec["s1"]["fired"] == 5
    assert rec["s2"]["fired"] == 5
    # s1 (priority winner) selected every time
    assert rec["s1"]["selected"] == 5
    assert rec["s2"]["selected"] == 0


# ---------------------------------------------------------------------------
# Callback routing
# ---------------------------------------------------------------------------


def test_on_trade_close_routes_to_originator() -> None:
    """Engine callback should reach the originating sub's listener."""
    s1 = _AlwaysFireStub("BUY", 5.0)
    s2 = _AlwaysFireStub("BUY", 1.0)
    composite = MultiStrategyComposite(
        [("s1", s1), ("s2", s2)],
        MultiStrategyConfig(conflict_policy="priority"),
    )
    composite.maybe_enter(_bar(0), [_bar(0)], 10_000.0, _config())
    fake_trade = Trade(
        entry_time=datetime(2026, 1, 1, tzinfo=UTC),
        exit_time=datetime(2026, 1, 1, 1, tzinfo=UTC),
        symbol="BTC",
        side="BUY",
        qty=1.0,
        entry_price=100.0,
        exit_price=103.0,
        pnl_r=1.5,
        pnl_usd=15.0,
        confluence_score=10.0,
        leverage_used=1.0,
        max_drawdown_during=0.0,
        regime="stub",
        exit_reason="target_hit",
    )
    composite.on_trade_close(fake_trade)
    # s1 won the priority conflict, so s1.on_trade_close should fire
    assert s1.closed == 1
    assert s2.closed == 0


def test_callback_isolation_when_sub_listener_raises() -> None:
    """A bad sub-listener can't break the composite."""

    @dataclass
    class _BadListener:
        fired: int = 0

        def maybe_enter(self, bar, hist, equity, config) -> _Open | None:
            self.fired += 1
            return _open("BUY", 5.0, bar.close, 1.0, 3.0)

        def on_trade_close(self, trade: Trade) -> None:
            raise RuntimeError("bad listener")

    bad = _BadListener()
    composite = MultiStrategyComposite([("bad", bad)])
    composite.maybe_enter(_bar(0), [_bar(0)], 10_000.0, _config())
    fake_trade = Trade(
        entry_time=datetime(2026, 1, 1, tzinfo=UTC),
        exit_time=datetime(2026, 1, 1, 1, tzinfo=UTC),
        symbol="BTC",
        side="BUY",
        qty=1.0,
        entry_price=100.0,
        exit_price=103.0,
        pnl_r=1.5,
        pnl_usd=15.0,
        confluence_score=10.0,
        leverage_used=1.0,
        max_drawdown_during=0.0,
        regime="stub",
        exit_reason="target_hit",
    )
    # Must not raise
    composite.on_trade_close(fake_trade)
