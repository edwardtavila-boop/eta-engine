"""Tests for portfolio_brain (Stream 1 of JARVIS Supercharge).

The portfolio brain wraps fleet-state lookups so per-bot consults
can be modulated by joint-fleet exposure, drawdown, and correlation
clusters. Tests cover each rule's threshold, the clamping bound,
and the snapshot graceful-fallback path.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import pytest

from eta_engine.brain.jarvis_v3 import portfolio_brain
from eta_engine.brain.jarvis_v3.portfolio_brain import (
    PortfolioContext,
    PortfolioVerdict,
    assess,
    snapshot,
)


@dataclass
class _FakeReq:
    """Minimal request shape expected by portfolio_brain.assess()."""

    bot_id: str = "test_bot"
    asset_class: str = "BTC"
    action: str = "ENTER"


def _ctx(**overrides) -> PortfolioContext:
    """Build a PortfolioContext, overriding only the fields provided."""
    base = dict(
        fleet_long_notional_by_asset={},
        fleet_short_notional_by_asset={},
        recent_entries_by_asset={},
        open_correlated_exposure=0.0,
        portfolio_drawdown_today_r=0.0,
        fleet_kill_active=False,
    )
    base.update(overrides)
    return PortfolioContext(**base)


def test_kill_blocks() -> None:
    """fleet_kill_active=True returns size_modifier=0 and block_reason."""
    ctx = _ctx(fleet_kill_active=True)
    verdict = assess(_FakeReq(), ctx)
    assert isinstance(verdict, PortfolioVerdict)
    assert verdict.size_modifier == 0.0
    assert verdict.block_reason == "fleet_kill_active"


def test_drawdown_tightens() -> None:
    """drawdown_today_r=-3.0 (more negative than -2.0) → modifier 0.5."""
    ctx = _ctx(portfolio_drawdown_today_r=-3.0)
    verdict = assess(_FakeReq(), ctx)
    assert verdict.block_reason is None
    assert verdict.size_modifier == pytest.approx(0.5, abs=1e-6)
    assert any("drawdown_tighten" in n for n in verdict.notes)


def test_correlated_exposure_tightens() -> None:
    """Same-asset notional > 30k → modifier multiplied by 0.7 (≤ 0.7)."""
    ctx = _ctx(fleet_long_notional_by_asset={"BTC": 50_000.0})
    verdict = assess(_FakeReq(asset_class="BTC"), ctx)
    assert verdict.block_reason is None
    assert verdict.size_modifier <= 0.7 + 1e-6
    assert any("correlated_exposure" in n for n in verdict.notes)


def test_correlation_cluster_blocks_size() -> None:
    """open_correlated_exposure > 0.75 → modifier multiplied by 0.6 (≤ 0.6)."""
    ctx = _ctx(open_correlated_exposure=0.80)
    verdict = assess(_FakeReq(), ctx)
    assert verdict.block_reason is None
    assert verdict.size_modifier <= 0.6 + 1e-6
    assert any("correlation_cluster_high" in n for n in verdict.notes)


def test_healthy_state_returns_1() -> None:
    """All-clear ctx → size_modifier == 1.0 and no block."""
    verdict = assess(_FakeReq(), _ctx())
    assert verdict.block_reason is None
    assert verdict.size_modifier == pytest.approx(1.0, abs=1e-6)


def test_size_modifier_clamped() -> None:
    """Combining every dampening rule still keeps modifier in [0.0, 1.5]."""
    ctx = _ctx(
        portfolio_drawdown_today_r=-5.0,
        fleet_long_notional_by_asset={"BTC": 100_000.0},
        open_correlated_exposure=0.90,
    )
    verdict = assess(_FakeReq(asset_class="BTC"), ctx)
    assert verdict.block_reason is None
    assert 0.0 <= verdict.size_modifier <= 1.5


def test_snapshot_returns_PortfolioContext(monkeypatch) -> None:  # noqa: N802 -- name fixed by plan brief
    """snapshot() returns a frozen PortfolioContext dataclass instance."""
    # Monkeypatch any external state lookups so the function is deterministic.
    fake_fleet = type(sys)("fake_fleet_allocator")
    fake_fleet.current_exposure = lambda: {
        "long_notional_by_asset": {"BTC": 10_000.0},
        "short_notional_by_asset": {},
        "recent_entries_by_asset": {"BTC": 1},
    }
    monkeypatch.setitem(
        sys.modules,
        "eta_engine.brain.jarvis_v3.fleet_allocator",
        fake_fleet,
    )
    ctx = snapshot()
    assert isinstance(ctx, PortfolioContext)
    # Frozen dataclass: mutation must raise.
    with pytest.raises((AttributeError, TypeError)):
        ctx.fleet_kill_active = True  # type: ignore[misc]


def test_snapshot_handles_missing_modules(monkeypatch) -> None:
    """If a wired module raises ImportError, snapshot() returns defaults."""

    def _explode(name: str, *_a, **_kw):
        raise ImportError(f"forced: {name}")

    # Patch portfolio_brain's internal import helpers so each wire fails.
    monkeypatch.setattr(portfolio_brain, "_safe_import", _explode)
    ctx = snapshot()
    assert isinstance(ctx, PortfolioContext)
    assert ctx.fleet_kill_active is False
    assert ctx.portfolio_drawdown_today_r == 0.0
    assert ctx.open_correlated_exposure == 0.0
    assert ctx.fleet_long_notional_by_asset == {}
    assert ctx.fleet_short_notional_by_asset == {}
