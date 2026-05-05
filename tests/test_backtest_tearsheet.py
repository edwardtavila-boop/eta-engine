"""Tests for ``eta_engine.backtest.tearsheet``.

Auto-scaffolded by scripts/_test_scaffold.py -- the import smoke and
the per-symbol smoke tests are boilerplate. Edit freely; the
operator-specific edge cases belong here.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.backtest.models import BacktestResult, Trade


def test_import_smoke() -> None:
    """Module imports without raising."""
    importlib.import_module("eta_engine.backtest.tearsheet")


def test_tearsheet_builder_smoke() -> None:
    """``TearsheetBuilder`` instantiates with no args (or skips if it requires args)."""
    from eta_engine.backtest.tearsheet import TearsheetBuilder

    try:
        obj = TearsheetBuilder()  # type: ignore[call-arg]
    except TypeError as e:
        pytest.skip(f"TearsheetBuilder requires args: {e}")
    else:
        assert obj is not None
        # TODO: real assertions about default state


def test_tearsheet_surfaces_oos_regime_performance_from_trade_labels() -> None:
    from eta_engine.backtest.tearsheet import TearsheetBuilder

    result = _result_with_trades([
        _trade(pnl_r=1.5, regime="trending_up", hours=1),
        _trade(pnl_r=-0.5, regime="trending_up", hours=2),
        _trade(pnl_r=-1.0, regime="chop", hours=3),
    ])

    sheet = TearsheetBuilder.from_result(result)

    assert "## OOS Regime Performance" in sheet
    assert "| trending_up | 2 | 50.0% | +0.500 | +1.000 |" in sheet
    assert "| chop | 1 | 0.0% | -1.000 | -1.000 |" in sheet


def test_tearsheet_falls_back_to_regime_state_contract_for_unlabeled_trades() -> None:
    from eta_engine.backtest.tearsheet import TearsheetBuilder

    result = _result_with_trades([
        _trade(symbol="MNQ", pnl_r=0.75, regime=None, hours=1),
        _trade(symbol="MNQ", pnl_r=0.25, regime=None, hours=2),
    ])
    regime_state = {
        "global_regime": "risk_on",
        "asset_regimes": {
            "MNQ": {"regime": "vol_compression", "confidence": 0.82},
        },
    }

    sheet = TearsheetBuilder.from_result(result, regime_state=regime_state)

    assert "Regime source: `regime_state.json` contract fallback" in sheet
    assert "| vol_compression | 2 | 100.0% | +0.500 | +1.000 |" in sheet


def _result_with_trades(trades: list[Trade]) -> BacktestResult:
    wins = [t for t in trades if t.pnl_r > 0]
    losses = [t for t in trades if t.pnl_r < 0]
    sum_win = sum(t.pnl_r for t in wins)
    sum_loss = abs(sum(t.pnl_r for t in losses))
    return BacktestResult(
        strategy_id="test-oos",
        n_trades=len(trades),
        win_rate=len(wins) / len(trades) if trades else 0.0,
        avg_win_r=sum_win / len(wins) if wins else 0.0,
        avg_loss_r=sum_loss / len(losses) if losses else 0.0,
        expectancy_r=sum(t.pnl_r for t in trades) / len(trades) if trades else 0.0,
        profit_factor=sum_win / sum_loss if sum_loss else 99.0,
        sharpe=1.0,
        sortino=1.0,
        max_dd_pct=2.5,
        total_return_pct=3.0,
        trades=trades,
    )


def _trade(
    *,
    pnl_r: float,
    regime: str | None,
    hours: int,
    symbol: str = "MNQ",
) -> Trade:
    entry = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=hours)
    return Trade(
        entry_time=entry,
        exit_time=entry + timedelta(minutes=15),
        symbol=symbol,
        side="BUY",
        qty=1.0,
        entry_price=20_000.0,
        exit_price=20_010.0,
        pnl_r=pnl_r,
        pnl_usd=pnl_r * 100.0,
        confluence_score=8.0,
        leverage_used=1.0,
        max_drawdown_during=0.0,
        regime=regime,
    )
