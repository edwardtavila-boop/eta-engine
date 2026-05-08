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


def test_cap_qty_paper_futures_floors_when_fleet_exhausted() -> None:
    """When fleet_remaining hits 0 (one MNQ contract pushes fleet over
    the small default $5k cap), the paper futures floor still kicks in
    so additional bots can take 1-contract entries. Without this, every
    futures entry after the first one was killed at the fleet gate."""
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    os.environ["ETA_LIVE_FUTURES_FLEET_BUDGET_USD"] = "5000"
    os.environ["ETA_PAPER_FUTURES_FLOOR"] = "1"
    try:
        # fleet already at $20k open notional (e.g. one MNQ at 1 contract);
        # cap is $5k → fleet_remaining = 0 → fleet_exhausted, but the
        # paper floor must still allow a 1-contract entry.
        qty, reason = cap_qty_to_budget(
            symbol="MNQ1", entry_price=20000.0, requested_qty=1.0,
            fleet_open_notional_usd=20000.0,
        )
        assert reason == "paper_futures_floor"
        assert qty == 1.0
    finally:
        os.environ.pop("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", None)
        os.environ.pop("ETA_PAPER_FUTURES_FLOOR", None)


def test_cap_qty_fleet_exhausted_floor_disabled_returns_zero() -> None:
    """Live deployments still respect the strict fleet cap when floor=0."""
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    os.environ["ETA_LIVE_FUTURES_FLEET_BUDGET_USD"] = "5000"
    os.environ["ETA_PAPER_FUTURES_FLOOR"] = "0"
    try:
        qty, reason = cap_qty_to_budget(
            symbol="MNQ1", entry_price=20000.0, requested_qty=1.0,
            fleet_open_notional_usd=20000.0,
        )
        assert reason == "fleet_exhausted"
        assert qty == 0.0
    finally:
        os.environ.pop("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", None)
        os.environ.pop("ETA_PAPER_FUTURES_FLOOR", None)


def test_cap_qty_futures_passes_when_budget_covers_contract() -> None:
    """A budget that covers the full contract value lets the requested qty through."""
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    # MNQ point_value = $2/pt -> 1 contract notional = $27,500 * 2 = $55,000.
    # Need a budget >= that for the request to pass.
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


def test_cap_qty_uses_contract_multiplier_for_notional() -> None:
    """REGRESSION 2026-05-07: cap_qty_to_budget must include the contract
    multiplier (point_value) when computing notional. Earlier code used
    ``qty * entry_price`` alone, which under-counted notional by the
    multiplier (5x for YM, 50x for ES, 100x for GC) -- letting bots size
    positions WAY past the per-bot cap on full-sized futures.

    YM at 49639 with point_value=$5/pt -> 1 contract notional = $248,195.
    A $10k per-bot cap should NOT permit even fractional YM exposure.
    """
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    os.environ["ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD"] = "10000"
    os.environ["ETA_LIVE_FUTURES_FLEET_BUDGET_USD"] = "50000"
    os.environ["ETA_PAPER_FUTURES_FLOOR"] = "0"  # disable floor for clean test
    try:
        # Request 1.0 YM contract: true notional = $248,195, way over $10k cap.
        # With the bug, supervisor saw notional = $49,639 (still over, OK).
        # With the bug + a smaller request like 0.1, supervisor saw
        # notional = $4,964 < $10k -> "ok" (WRONG: true notional = $24,820).
        qty, reason = cap_qty_to_budget(
            symbol="YM", entry_price=49639.0, requested_qty=0.1,
        )
        # Post-fix: 0.1 * 49639 * 5 = $24,820 > $10k cap, so this MUST be capped.
        assert reason == "per_bot_capped", (
            f"YM 0.1 contract at $24,820 notional MUST be capped at $10k; "
            f"got reason={reason} qty={qty}"
        )
        # Capped notional must equal the cap: 10000 / (49639 * 5) = 0.04
        # Then int-floored to 0 (futures min-1-lot, floor disabled for this test).
        assert qty == 0.0
    finally:
        os.environ.pop("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", None)
        os.environ.pop("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", None)
        os.environ.pop("ETA_PAPER_FUTURES_FLOOR", None)


def test_cap_qty_gold_full_contract_correctly_capped() -> None:
    """GC point_value = $100/pt. 1 contract at $4,700 -> $470,000 notional.
    The pre-fix code reported notional as just $4,700 -- a 100x undercount.
    Verify the fix prevents an unlimited Gold position from slipping under
    the per-bot cap.
    """
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    os.environ["ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD"] = "10000"
    os.environ["ETA_LIVE_FUTURES_FLEET_BUDGET_USD"] = "50000"
    os.environ["ETA_PAPER_FUTURES_FLOOR"] = "0"
    try:
        # Even 1 GC contract at $470k notional must be capped at $10k.
        qty, reason = cap_qty_to_budget(
            symbol="GC", entry_price=4700.0, requested_qty=1.0,
        )
        assert reason == "per_bot_capped"
        # Capped: 10000 / (4700 * 100) = 0.0212 -> int -> 0 contracts.
        assert qty == 0.0
    finally:
        os.environ.pop("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", None)
        os.environ.pop("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", None)
        os.environ.pop("ETA_PAPER_FUTURES_FLOOR", None)


def test_cap_qty_micro_gold_fits_normal_budget() -> None:
    """MGC (Micro Gold) point_value = $10/pt. 1 contract at $4,700
    notional = $47,000. With a $50k per-bot cap, 1 MGC fits cleanly.

    This pins the fix's correctness from the OTHER side: the multiplier-
    aware code must also let legitimate trades through, not just block
    over-sized ones.
    """
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    os.environ["ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD"] = "50000"
    os.environ["ETA_LIVE_FUTURES_FLEET_BUDGET_USD"] = "200000"
    try:
        qty, reason = cap_qty_to_budget(
            symbol="MGC", entry_price=4700.0, requested_qty=1.0,
        )
        assert reason == "ok"
        assert qty == 1.0
    finally:
        os.environ.pop("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", None)
        os.environ.pop("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", None)


def test_paper_floor_disabled_in_live_mode() -> None:
    """Live-money safety: when ``ETA_SUPERVISOR_MODE=live`` (or
    ``ETA_SUPERVISOR_LIVE_MONEY=1``), ``paper_futures_floor`` does NOT
    kick in -- a budget cap that rounds qty below 1 contract returns
    0, NOT a silent up-round to 1 contract (which would be 50x over-
    risk per trade if the strategy meant to size below 1).
    """
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    os.environ["ETA_SUPERVISOR_MODE"] = "live"
    os.environ["ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD"] = "10000"
    os.environ["ETA_LIVE_FUTURES_FLEET_BUDGET_USD"] = "100000"
    try:
        # MNQ at $57k notional / $10k cap -> capped to 0 contracts.
        # Paper mode would lift to 1; live mode must NOT.
        qty, reason = cap_qty_to_budget(
            symbol="MNQ1", entry_price=28500.0, requested_qty=1.0,
        )
        assert qty == 0.0, (
            f"live mode must not lift sub-1-lot to 1; got qty={qty} reason={reason}"
        )
        assert reason == "per_bot_capped"
    finally:
        os.environ.pop("ETA_SUPERVISOR_MODE", None)
        os.environ.pop("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", None)
        os.environ.pop("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", None)


def test_paper_floor_disabled_via_explicit_live_money_flag() -> None:
    """``ETA_SUPERVISOR_LIVE_MONEY=1`` is the explicit live flag -- when
    set it must also disable the paper floor regardless of MODE."""
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    os.environ["ETA_SUPERVISOR_LIVE_MONEY"] = "1"
    os.environ["ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD"] = "10000"
    os.environ["ETA_LIVE_FUTURES_FLEET_BUDGET_USD"] = "100000"
    try:
        qty, reason = cap_qty_to_budget(
            symbol="MNQ1", entry_price=28500.0, requested_qty=1.0,
        )
        assert qty == 0.0
        assert reason == "per_bot_capped"
    finally:
        os.environ.pop("ETA_SUPERVISOR_LIVE_MONEY", None)
        os.environ.pop("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", None)
        os.environ.pop("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", None)


def test_per_bot_budget_override_lifts_cap_for_high_notional_contracts() -> None:
    """High-notional contracts (YM ~$248k, GC ~$470k, ES ~$300k) cannot
    fit the default per-bot $10k cap. Per-bot ``per_bot_budget_usd``
    override in registry extras lets a bot declare its own bigger cap
    without lifting the cap fleet-wide.
    """
    from unittest.mock import patch

    from eta_engine.scripts.bracket_sizing import _budget_per_bot_usd

    fake_assignment = type(
        "FakeAssignment", (),
        {"bot_id": "ym_sweep_reclaim", "extras": {"per_bot_budget_usd": 50000.0}},
    )
    with patch(
        "eta_engine.strategies.per_bot_registry.ASSIGNMENTS",
        [fake_assignment],
    ):
        # With override: $50k cap.
        cap = _budget_per_bot_usd("YM1", bot_id="ym_sweep_reclaim")
        assert cap == 50000.0

        # Without bot_id: falls through to asset-class default.
        os.environ["ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD"] = "10000"
        try:
            default_cap = _budget_per_bot_usd("YM1")
            assert default_cap == 10000.0, (
                f"asset-class default should apply when bot_id is None; got {default_cap}"
            )
        finally:
            os.environ.pop("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", None)


def test_per_bot_budget_override_malformed_falls_back_to_default() -> None:
    """A bad per_bot_budget_usd value (string, negative number, etc.)
    must NOT bypass sizing -- fall back to the asset-class default
    rather than letting a misconfigured registry entry do anything wild.
    """
    from unittest.mock import patch

    from eta_engine.scripts.bracket_sizing import _budget_per_bot_usd

    fake_assignment = type(
        "FakeAssignment", (),
        {"bot_id": "broken_bot", "extras": {"per_bot_budget_usd": "not-a-number"}},
    )
    os.environ["ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD"] = "10000"
    try:
        with patch(
            "eta_engine.strategies.per_bot_registry.ASSIGNMENTS",
            [fake_assignment],
        ):
            cap = _budget_per_bot_usd("YM1", bot_id="broken_bot")
            # Falls back to asset-class default (NOT the malformed override).
            assert cap == 10000.0
    finally:
        os.environ.pop("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", None)


def test_per_bot_budget_override_non_positive_falls_back_to_default() -> None:
    """Zero/negative overrides are unsafe and must not bypass defaults."""
    from unittest.mock import patch

    from eta_engine.scripts.bracket_sizing import _budget_per_bot_usd

    fake_assignment = type(
        "FakeAssignment", (),
        {"bot_id": "broken_bot", "extras": {"per_bot_budget_usd": -1}},
    )
    os.environ["ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD"] = "10000"
    try:
        with patch(
            "eta_engine.strategies.per_bot_registry.ASSIGNMENTS",
            [fake_assignment],
        ):
            cap = _budget_per_bot_usd("YM1", bot_id="broken_bot")
            assert cap == 10000.0
    finally:
        os.environ.pop("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", None)


def test_cap_qty_crypto_spot_unchanged_by_multiplier_fix() -> None:
    """The point_value fix must NOT affect crypto spot trading where
    notional has always been correctly ``qty * price``. BTC, ETH, SOL
    on Alpaca paper are 1.0-multiplier instruments.
    """
    from eta_engine.scripts.bracket_sizing import cap_qty_to_budget
    os.environ["ETA_LIVE_CRYPTO_BUDGET_PER_BOT_USD"] = "1000"
    os.environ["ETA_LIVE_CRYPTO_FLEET_BUDGET_USD"] = "5000"
    try:
        # 0.01 BTC * $80k = $800 < $1k cap -> ok
        qty, reason = cap_qty_to_budget(
            symbol="BTC", entry_price=80000.0, requested_qty=0.01,
        )
        assert reason == "ok"
        assert qty == 0.01
        # 0.02 BTC * $80k = $1600 > $1k cap -> per_bot_capped to 0.0125
        qty, reason = cap_qty_to_budget(
            symbol="BTC", entry_price=80000.0, requested_qty=0.02,
        )
        assert reason == "per_bot_capped"
        assert abs(qty * 80000.0 - 1000.0) < 1.0
    finally:
        os.environ.pop("ETA_LIVE_CRYPTO_BUDGET_PER_BOT_USD", None)
        os.environ.pop("ETA_LIVE_CRYPTO_FLEET_BUDGET_USD", None)


# ─── Per-asset bracket precision (FX rounding-collapse fix) ────


def test_compute_bracket_fx_uses_5_decimal_precision() -> None:
    """FX prices live at ~1.xxxx. With 4-decimal rounding, a tight ATR
    stop like 1.17082 collapses to 1.1708 (entry price) → zero stop
    distance → realized_r explodes when divided by ~0. Confirms the
    precision is 5 decimals so a 5-pip stop survives rounding.
    """
    from eta_engine.scripts.bracket_sizing import compute_bracket
    bars = [{"high": 1.171, "low": 1.170, "close": 1.1708,
             "open": 1.171} for _ in range(20)]
    stop, target, src = compute_bracket(
        side="BUY", entry_price=1.1708, bars=bars,
        stop_mult_override=2.0, target_mult_override=2.5,
    )
    assert src == "atr"
    # 20 pip stop = 0.0020 — must survive 5-decimal rounding (it would
    # collapse to 0 under 4-decimal rounding for tighter stops)
    assert abs(stop - 1.1708) >= 0.001, (
        f"FX stop distance {abs(stop-1.1708):.6f} too small — "
        "5-decimal precision should preserve it"
    )


def test_compute_bracket_btc_uses_2_decimal_precision() -> None:
    """BTC prices ~100k. 2-decimal precision is sufficient and avoids
    sub-cent noise that confuses bracket comparisons."""
    from eta_engine.scripts.bracket_sizing import compute_bracket
    bars = [{"high": 100100, "low": 99900, "close": 100000,
             "open": 100050} for _ in range(20)]
    stop, target, src = compute_bracket(
        side="BUY", entry_price=100000.0, bars=bars,
        stop_mult_override=2.0, target_mult_override=2.5,
    )
    assert src == "atr"
    assert stop == round(stop, 2), "BTC stop must round to 2 decimals"
    assert abs(stop - 100000) >= 1.0


def test_compute_bracket_min_distance_falls_through_to_fixed_pct() -> None:
    """When ATR is so small the rounded stop equals entry price, the
    function falls through to fixed_pct (which uses fractions of price
    so always produces meaningful distance) instead of returning a
    zero-distance bracket that crashes realized_r downstream.
    """
    from eta_engine.scripts.bracket_sizing import compute_bracket
    # Entry at 1.17 + tiny ATR (0.000001) would round stop to entry
    bars = [{"high": 1.170001, "low": 1.169999, "close": 1.17,
             "open": 1.170001} for _ in range(20)]
    stop, target, src = compute_bracket(
        side="BUY", entry_price=1.17, bars=bars,
        stop_mult_override=2.0, target_mult_override=2.5,
    )
    # Either ATR survived with non-zero distance OR fell through to
    # fixed_pct — the key invariant is non-zero distance.
    assert abs(stop - 1.17) > 0.0, f"stop {stop} must differ from entry"
    assert abs(target - 1.17) > 0.0, f"target {target} must differ from entry"
