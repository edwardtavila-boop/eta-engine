"""
EVOLUTIONARY TRADING ALGO  //  tests.conftest
=================================
Shared fixtures for the full test suite.
"""

from __future__ import annotations

import pytest

from eta_engine.funnel.equity_monitor import BotEquity, PortfolioState

# ---------------------------------------------------------------------------
# Market data fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_bar() -> dict[str, float]:
    """Standard 5-min OHLCV bar for MNQ."""
    return {
        "open": 21550.0,
        "high": 21575.0,
        "low": 21530.0,
        "close": 21560.0,
        "volume": 12345.0,
        "atr_14": 18.5,
    }


@pytest.fixture()
def sample_config() -> dict[str, float]:
    """Standard risk config values."""
    return {
        "equity": 50_000.0,
        "risk_pct": 0.01,
        "daily_loss_cap_pct": 0.025,
        "max_dd_kill_pct": 0.08,
        "price": 21550.0,
        "atr": 18.5,
    }


@pytest.fixture()
def sample_portfolio_state() -> PortfolioState:
    """Portfolio with 3 bots for integration tests."""
    bots = {
        "mnq_engine": BotEquity(
            bot_name="mnq_engine",
            current_equity=55_000.0,
            peak_equity=58_000.0,
            baseline_usd=50_000.0,
            excess_usd=5_000.0,
            todays_pnl=350.0,
        ),
        "eth_perp": BotEquity(
            bot_name="eth_perp",
            current_equity=12_000.0,
            peak_equity=12_500.0,
            baseline_usd=10_000.0,
            excess_usd=2_000.0,
            todays_pnl=-120.0,
        ),
        "sol_perp": BotEquity(
            bot_name="sol_perp",
            current_equity=8_000.0,
            peak_equity=8_200.0,
            baseline_usd=7_500.0,
            excess_usd=500.0,
            todays_pnl=45.0,
        ),
    }
    return PortfolioState(
        bots=bots,
        total_equity=75_000.0,
        total_excess=7_500.0,
        total_pnl_today=275.0,
    )
