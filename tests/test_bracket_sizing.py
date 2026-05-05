"""Tests for ATR brackets and per-class capital budgets."""
from __future__ import annotations

import os  # noqa: I001  -- single import; ruff's "unsorted" warning is spurious here

# ─── ATR ─────────────────────────────────────────────────────────


def test_compute_atr_returns_none_with_too_few_bars() -> None:
    from eta_engine.scripts.bracket_sizing import compute_atr
    bars = [{"high": 100, "low": 99, "close": 99.5}] * 5
    assert compute_atr(bars, period=14) is None


def test_compute_atr_handles_simple_window() -> None:
    """Hand-computed ATR on a synthetic 16-bar series."""
    from eta_engine.scripts.bracket_sizing import compute_atr
    # 16 bars with constant range so ATR equals the range
    bars = [{"high": 105, "low": 95, "close": 100}] * 16
    atr = compute_atr(bars, period=14)
    assert atr is not None
    # TR for each bar (after the first): max(high-low=10, |high-prev_close|=5, |low-prev_close|=5) = 10
    assert abs(atr - 10.0) < 1e-6


def test_compute_atr_uses_correct_period_window() -> None:
    """Older bars outside the period window must NOT influence ATR."""
    from eta_engine.scripts.bracket_sizing import compute_atr
    # 5 ancient quiet bars then 15 active bars
    bars = (
        [{"high": 100.1, "low": 99.9, "close": 100.0}] * 5
        + [{"high": 110, "low": 90, "close": 100}] * 15
    )
    atr = compute_atr(bars, period=14)
    assert atr is not None
    # Last 14 TRs are all from the active region (range=20)
    # First "active" bar's TR uses prev_close=100.0 so TR = max(20, 10, 10) = 20
    assert atr > 15.0


# ─── Bracket sizing ──────────────────────────────────────────────


def test_compute_bracket_uses_atr_when_available() -> None:
    from eta_engine.scripts.bracket_sizing import compute_bracket
    bars = [{"high": 105, "low": 95, "close": 100}] * 16  # ATR = 10
    stop, target, source = compute_bracket(
        side="BUY", entry_price=100.0, bars=bars,
    )
    assert source == "atr"
    # Defaults: stop_mult=2.0, target_mult=3.0
    assert abs(stop - 80.0) < 1e-6  # 100 - 2*10
    assert abs(target - 130.0) < 1e-6  # 100 + 3*10


def test_compute_bracket_falls_back_to_fixed_pct_without_bars() -> None:
    from eta_engine.scripts.bracket_sizing import compute_bracket
    stop, target, source = compute_bracket(
        side="BUY", entry_price=100.0, bars=None,
    )
    assert source == "fixed_pct"
    assert abs(stop - 98.5) < 1e-6  # 100 * (1 - 0.015)
    assert abs(target - 102.0) < 1e-6  # 100 * (1 + 0.020)


def test_compute_bracket_inverts_for_sell() -> None:
    from eta_engine.scripts.bracket_sizing import compute_bracket
    bars = [{"high": 105, "low": 95, "close": 100}] * 16
    stop, target, source = compute_bracket(
        side="SELL", entry_price=100.0, bars=bars,
    )
    assert source == "atr"
    # Short: stop ABOVE entry, target BELOW entry
    assert stop > 100.0
    assert target < 100.0
    assert abs(stop - 120.0) < 1e-6  # 100 + 2*10
    assert abs(target - 70.0) < 1e-6  # 100 - 3*10


def test_compute_bracket_honors_env_overrides() -> None:
    from eta_engine.scripts.bracket_sizing import compute_bracket
    os.environ["ETA_BRACKET_ATR_STOP_MULT"] = "1.5"
    os.environ["ETA_BRACKET_ATR_TARGET_MULT"] = "2.5"
    try:
        bars = [{"high": 105, "low": 95, "close": 100}] * 16  # ATR = 10
        stop, target, _ = compute_bracket(
            side="BUY", entry_price=100.0, bars=bars,
        )
        assert abs(stop - 85.0) < 1e-6  # 100 - 1.5*10
        assert abs(target - 125.0) < 1e-6  # 100 + 2.5*10
    finally:
        os.environ.pop("ETA_BRACKET_ATR_STOP_MULT", None)
        os.environ.pop("ETA_BRACKET_ATR_TARGET_MULT", None)


# ─── Capital budget caps ────────────────────────────────────────


def test_cap_qty_passes_through_when_under_budget() -> None:
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    # Default crypto per-bot budget is $100; 0.001 BTC * $80k = $80 < $100
    qty, reason = cap_qty_to_budget(
        symbol="BTC", entry_price=80000.0, requested_qty=0.001,
    )
    assert reason == "ok"
    assert qty == 0.001


def test_cap_qty_caps_at_per_bot_budget() -> None:
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    # Default crypto per-bot budget is $100; request 0.01 BTC * $80k = $800 → cap to $100
    qty, reason = cap_qty_to_budget(
        symbol="BTC", entry_price=80000.0, requested_qty=0.01,
    )
    assert reason == "per_bot_capped"
    # Capped qty notional should equal $100
    assert abs(qty * 80000.0 - 100.0) < 1.0


def test_cap_qty_caps_at_fleet_budget() -> None:
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    # Default crypto fleet budget is $1500; existing exposure $1450 → only $50 left
    qty, reason = cap_qty_to_budget(
        symbol="BTC", entry_price=80000.0, requested_qty=0.01,
        fleet_open_notional_usd=1450.0,
    )
    assert reason == "fleet_capped"
    assert abs(qty * 80000.0 - 50.0) < 1.0


def test_cap_qty_zero_when_fleet_exhausted() -> None:
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    qty, reason = cap_qty_to_budget(
        symbol="BTC", entry_price=80000.0, requested_qty=0.01,
        fleet_open_notional_usd=2000.0,  # over the $1500 cap
    )
    assert reason == "fleet_exhausted"
    assert qty == 0.0


def test_cap_qty_paper_futures_floors_to_one_contract() -> None:
    """Paper-mode futures floor: when the budget cap rounds qty to 0 but
    the operator asked for ≥1 contract, return 1.0 (not 0.0). Without
    this floor every futures entry approved by JARVIS would die at the
    cap because $500/MNQ_$40k_notional = 0.0125 → int → 0. Symptom in
    production: 82 APPROVED verdicts for bot.mnq, zero n_entries.
    """
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    # $20k per-bot budget vs MNQ1 $27.5k notional → 0.72 contracts →
    # int floor → 0 → paper_futures_floor lifts to 1.
    os.environ["ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD"] = "20000"
    os.environ["ETA_LIVE_FUTURES_FLEET_BUDGET_USD"] = "100000"
    os.environ["ETA_PAPER_FUTURES_FLOOR"] = "1"
    try:
        qty, reason = cap_qty_to_budget(
            symbol="MNQ1", entry_price=27500.0, requested_qty=1.0,
        )
        assert reason == "paper_futures_floor"
        assert qty == 1.0
    finally:
        os.environ.pop("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", None)
        os.environ.pop("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", None)
        os.environ.pop("ETA_PAPER_FUTURES_FLOOR", None)


def test_cap_qty_paper_futures_floor_disabled_returns_zero() -> None:
    """Live deployments set ETA_PAPER_FUTURES_FLOOR=0 to restore the
    strict cap behavior. Confirm the floor opt-out works."""
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    os.environ["ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD"] = "20000"
    os.environ["ETA_LIVE_FUTURES_FLEET_BUDGET_USD"] = "100000"
    os.environ["ETA_PAPER_FUTURES_FLOOR"] = "0"
    try:
        qty, reason = cap_qty_to_budget(
            symbol="MNQ1", entry_price=27500.0, requested_qty=1.0,
        )
        assert reason == "per_bot_capped"
        assert qty == 0.0
    finally:
        os.environ.pop("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", None)
        os.environ.pop("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", None)
        os.environ.pop("ETA_PAPER_FUTURES_FLOOR", None)


def test_cap_qty_futures_passes_when_budget_covers_contract() -> None:
    """A budget that covers the full contract value lets the requested qty through."""
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    os.environ["ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD"] = "60000"
    os.environ["ETA_LIVE_FUTURES_FLEET_BUDGET_USD"] = "100000"
    try:
        qty, reason = cap_qty_to_budget(
            symbol="MNQ1", entry_price=27500.0, requested_qty=1.0,
        )
        assert reason == "ok"
        assert qty == 1.0
    finally:
        os.environ.pop("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", None)
        os.environ.pop("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", None)
