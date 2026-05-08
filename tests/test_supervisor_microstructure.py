"""Microstructure-correctness tests for the JARVIS strategy supervisor.

Pins the four fixes landed for paper-sim and bracket-pricing paths:

1. Slippage sign — adverse selection, never beneficial, regardless of side.
2. Paper-sim exit fills at the bracket leg price (stop_price ± slip,
   target_price exactly), not at ``bar.close``.
3. ``_maybe_exit`` checks intrabar high/low against bracket levels so
   wickers and target-touches don't latch into the next bar.
4. Tick-grid rounding via ``_round_to_tick`` for real-broker prices.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─── Fix 1: Slippage sign ────────────────────────────────────────


def test_buy_entry_slippage_is_adverse_above_ref(tmp_path: Path) -> None:
    """A BUY entry must fill ABOVE the reference (mid) price.

    Real BUYs cross the offer; adverse-selection slippage is positive.
    Uses SOL (tick=0.01) so the 1.5 bps slip on ref=100 doesn't get
    snapped back to mid by tick-grid rounding (BTC uses tick=5.0).
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="test_buy_slip", symbol="SOL", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bar = {"close": 100.0, "high": 100.5, "low": 99.5, "open": 99.8}
    rec = router.submit_entry(
        bot=bot, signal_id="sig_buy", side="BUY", bar=bar, size_mult=1.0,
    )
    assert rec is not None
    assert rec.fill_price > 100.0, (
        f"BUY at ref=100 must fill above mid (adverse), got {rec.fill_price}"
    )


def test_sell_entry_slippage_is_adverse_below_ref(tmp_path: Path) -> None:
    """A SELL entry must fill BELOW the reference (mid) price.

    Pins the sign-flip regression: previously SELLs were filling at
    ``ref * (1 - 1.5/10000)`` — a BETTER-than-mid price for the trader,
    biasing every short trade by ~3 bps round-trip.
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="test_sell_slip", symbol="SOL", strategy_kind="x",
        direction="short", cash=5000.0,
    )
    bar = {"close": 100.0, "high": 100.5, "low": 99.5, "open": 99.8}
    rec = router.submit_entry(
        bot=bot, signal_id="sig_sell", side="SELL", bar=bar, size_mult=1.0,
    )
    assert rec is not None
    assert rec.fill_price < 100.0, (
        f"SELL at ref=100 must fill below mid (adverse), got {rec.fill_price}"
    )


# ─── Fix 2 + Fix 3: Paper-sim exits at bracket levels (intrabar) ──


def test_paper_exit_at_stop_fills_at_stop_price_not_close(
    tmp_path: Path,
) -> None:
    """LONG exit triggered by paper_stop should fill ~stop, not bar.close.

    Bar configured: low pierces stop, close above stop. Without Fix 2
    the exit would fill at close (over-booking the trade). With Fix 2
    the exit fills at stop_price minus a small adverse slip.
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="test_long_stop", symbol="SOL", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bot.open_position = {
        "side": "BUY",
        "qty": 1.0,
        "entry_price": 100.0,
        "entry_ts": "2026-05-05T00:00:00+00:00",
        "signal_id": "sig_long",
        "bracket_stop": 99.0,
        "bracket_target": 110.0,
        "exit_reason": "paper_stop",
    }
    # Bar pierces stop intrabar (low=98.5) but closes back above it.
    exit_bar = {"close": 99.5, "high": 99.9, "low": 98.5, "open": 99.6}
    rec = router.submit_exit(bot=bot, bar=exit_bar)
    assert rec is not None
    # Fill should be at ~99 (stop) with adverse slippage subtracted
    # for SELL: 99 - 99 * 1.5/10000 = 99 - 0.01485 ≈ 98.985, then
    # tick-rounded to SOL's 0.01 grid → 98.99.
    assert 98.95 <= rec.fill_price <= 99.0, (
        f"paper_stop fill should be ~99 (stop) minus adverse slippage, "
        f"not bar.close=99.5; got {rec.fill_price}"
    )


def test_paper_exit_at_target_uses_intrabar_high(tmp_path: Path) -> None:
    """A LONG bracket target should fire when bar.high pierces target.

    Without Fix 3, this bar (close=110, target=110.4 — below close)
    would actually fire because close >= target, but the harder case
    is high>=target while close<target. We pin both directions here:
    bar with high=110.5, target=110.4, close=110.3 → must fire.
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )
    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="test_long_target_intrabar", symbol="SOL", strategy_kind="x",
        direction="long", cash=5000.0,
    )
    bot.open_position = {
        "side": "BUY",
        "qty": 1.0,
        "entry_price": 100.0,
        "entry_ts": "2026-05-05T00:00:00+00:00",
        "signal_id": "sig_long_t",
        "bracket_stop": 99.0,
        "bracket_target": 110.4,
    }
    # Build a tiny supervisor instance just to call _maybe_exit. We
    # bypass __init__ to avoid loading the full bot registry/JARVIS.
    sup = JarvisStrategySupervisor.__new__(JarvisStrategySupervisor)
    sup._router = router
    captured = {}

    def capture_close(_bot, rec, **_kwargs) -> None:
        captured["rec"] = rec

    sup._propagate_close = capture_close  # type: ignore[attr-defined]
    # Bar pierces target intrabar (high=110.5) but closes below
    # (close=110.3). Without Fix 3, cur_price=110.3 < target=110.4 →
    # no exit. With Fix 3, bar.high=110.5 >= target → exit fires.
    exit_bar = {"close": 110.3, "high": 110.5, "low": 110.0, "open": 110.1}
    sup._maybe_exit(bot, exit_bar)
    # After Fix 3, _maybe_exit should have closed the position via
    # the paper_target path → fill at target_price exactly.
    assert bot.open_position is None, (
        "Fix 3: bar.high >= target should trigger paper_target exit "
        "even when close < target"
    )
    assert captured["rec"].fill_price == 110.4


# ─── Fix 4: Tick-grid rounding ────────────────────────────────────


def test_round_to_tick_mnq_quarter_grid() -> None:
    """MNQ has tick_size=0.25; 21437.83 → nearest quarter (21437.75)."""
    from eta_engine.scripts.jarvis_strategy_supervisor import _round_to_tick

    rounded = _round_to_tick(21437.83, "MNQ")
    # 21437.83 / 0.25 = 85751.32 → round() = 85751 → 85751 * 0.25 = 21437.75
    assert rounded == 21437.75, (
        f"21437.83 should round to 21437.75 on MNQ 0.25-tick grid; got {rounded}"
    )


def test_round_to_tick_unknown_symbol_passthrough() -> None:
    """Already-on-grid prices return unchanged (no spurious rounding noise).

    The default-spec branch in get_spec uses tick=0.25, so we check a
    value that's already on the 0.25 grid — it must pass through
    untouched (modulo float32 noise).
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import _round_to_tick

    # 100.50 is already on a 0.25 grid (100.50 / 0.25 = 402.0)
    rounded = _round_to_tick(100.50, "ZZZ_UNKNOWN_SYMBOL")
    assert abs(rounded - 100.50) < 1e-9, (
        f"on-grid price should round-trip unchanged; got {rounded}"
    )
    # And a price that's already on the GC 0.10 tick grid via real spec:
    rounded_gc = _round_to_tick(2050.50, "GC")
    assert abs(rounded_gc - 2050.50) < 1e-9, (
        f"on-grid GC price should round-trip; got {rounded_gc}"
    )
