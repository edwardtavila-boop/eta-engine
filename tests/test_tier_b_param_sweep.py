"""Tests for scripts.tier_b_param_sweep."""

from __future__ import annotations

from eta_engine.scripts import tier_b_param_sweep as mod


def _cell(
    bot: str, conf: float, risk: float, exp_r: float, dd: float = 5.0, trades: int = 100, gate_pass: bool | None = None
) -> mod.SweepCell:
    return mod.SweepCell(
        bot=bot,
        confluence_threshold=conf,
        risk_per_trade_pct=risk,
        n_trades=trades,
        win_rate=0.5,
        expectancy_r=exp_r,
        max_dd_pct=dd,
        total_return_pct=10.0,
        gate_pass=(exp_r >= 0.30 and dd <= 15.0) if gate_pass is None else gate_pass,
    )


def test_winner_picks_highest_expectancy_among_passers():
    cells = [
        _cell("eth_perp", 5.0, 0.01, 0.32),
        _cell("eth_perp", 6.0, 0.01, 0.45),
        _cell("eth_perp", 7.0, 0.01, 0.28),  # fails
    ]
    w = mod._winner(cells)
    assert w is not None
    assert w.expectancy_r == 0.45
    assert w.gate_pass is True


def test_winner_tiebreaks_on_dd_when_expectancy_equal():
    cells = [
        _cell("eth_perp", 5.0, 0.01, 0.40, dd=10.0),
        _cell("eth_perp", 6.0, 0.01, 0.40, dd=4.0),  # lower dd wins
        _cell("eth_perp", 7.0, 0.01, 0.40, dd=12.0),
    ]
    w = mod._winner(cells)
    assert w is not None
    assert w.max_dd_pct == 4.0


def test_winner_fallback_to_closest_to_passing_when_none_pass():
    cells = [
        _cell("xrp_perp", 5.0, 0.01, 0.10),
        _cell("xrp_perp", 6.0, 0.01, 0.25),
        _cell("xrp_perp", 7.0, 0.01, 0.15),
    ]
    w = mod._winner(cells)
    assert w is not None
    assert w.expectancy_r == 0.25
    assert w.gate_pass is False


def test_winner_empty_returns_none():
    assert mod._winner([]) is None


def test_grid_cardinality_matches_advertised_shape():
    # 7 conf × 4 risk = 28 per bot, 4 bots = 112 cells total
    assert len(mod.CONFLUENCE_GRID) == 7
    assert len(mod.RISK_GRID) == 4
    assert len(mod.TIER_B_BOTS) == 4
    assert len(mod.TIER_B_BOTS) * len(mod.CONFLUENCE_GRID) * len(mod.RISK_GRID) == 112
