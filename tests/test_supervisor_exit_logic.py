"""Supervisor exit-logic enhancement tests.

Pins the contracts for the three new features added to
``jarvis_strategy_supervisor`` on the
``codex/paper-live-runtime-hardening`` branch:

1. **Trailing stop** — once a position reaches ``ETA_TRAILING_STOP_ACTIVATE_R``
   profit (default 1.0 R), the bracket_stop is moved to entry (BE) and
   then trails further as price extends. The stop NEVER moves adversely.
2. **Partial profit-taking** — when an open position reaches
   ``ETA_PARTIAL_PROFIT_R`` profit, ``ETA_PARTIAL_PROFIT_PCT`` of the
   size is closed via a reduce_only exit and the remainder rides on.
3. **Cross-bot signal aggregation** — when N>=2 alpha bots fire same
   (symbol, direction) entries inside ``ETA_BOT_AGGREGATION_WINDOW_S``,
   only the FIRST one routes; subsequent siblings get a structured
   ``consolidated_with_<first_bot_id>`` rejection on their heartbeat.

All three behaviors are env-flagged so they can be disabled if they
misbehave; the tests cover both the on (default) and off paths where
applicable.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_supervisor(tmp_path: Path):
    """Build a JarvisStrategySupervisor with a tmp state dir.

    No bots loaded, no JARVIS bootstrap, no broker — bare metal, suitable
    for unit-testing the helper methods directly.
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.state_dir = tmp_path
    cfg.broker_router_pending_dir = tmp_path / "pending"
    cfg.broker_router_pending_dir.mkdir(parents=True, exist_ok=True)
    cfg.mode = "paper_sim"
    cfg.data_feed = "mock"
    return JarvisStrategySupervisor(cfg=cfg)


def _make_bot(bot_id: str = "alpha", symbol: str = "MNQ"):
    """Build a BotInstance with a typical LONG open position.

    Position layout:
        entry=18_000  stop=17_900  target=18_100  (1R = 100 pts)
        side=BUY      qty=2
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import BotInstance

    bot = BotInstance(
        bot_id=bot_id, symbol=symbol, strategy_kind="futures",
        direction="long", cash=5_000.0,
    )
    bot.open_position = {
        "side": "BUY",
        "qty": 2.0,
        "entry_price": 18_000.0,
        "entry_ts": "2026-05-06T11:00:00+00:00",
        "signal_id": f"{bot_id}_entry",
        "bracket_stop": 17_900.0,
        "bracket_target": 18_200.0,  # 2R target so trailing/partial fire before target hits
    }
    return bot


# ---------------------------------------------------------------------------
# 1. Trailing stop activates at 1R
# ---------------------------------------------------------------------------


def test_trailing_stop_activates_at_1R(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At +1R unrealized, the stop should move to entry (breakeven).

    Setup: LONG at 18000, stop 17900 (100 pts = 1R), target 18200 (2R).
    A bar with high=18100 prints exactly +1R unrealized. Before the
    helper runs, bracket_stop = 17900. After, bracket_stop should be
    raised to entry (18000) and ``trailing_active`` should be True.
    """
    monkeypatch.setenv("ETA_TRAILING_STOP_ENABLED", "true")
    monkeypatch.setenv("ETA_TRAILING_STOP_ACTIVATE_R", "1.0")

    sup = _make_supervisor(tmp_path)
    bot = _make_bot()
    pos = bot.open_position

    # Bar with high == +1R; close lower so we test the high-trigger path.
    bar = {"open": 18_050.0, "high": 18_100.0, "low": 18_040.0, "close": 18_050.0}

    # Sanity: pre-trail stop is at the entry-stop distance.
    assert pos["bracket_stop"] == 17_900.0
    assert pos.get("trailing_active") is not True

    sup._maybe_apply_trailing_stop(bot, pos, bar)

    # At exactly +1R, excess_r=0 → new_stop=entry=18000 (BE).
    assert pos["trailing_active"] is True, "trailing_active flag must be set"
    assert pos["bracket_stop"] == pytest.approx(18_000.0), (
        f"stop should move to entry at +1R, got {pos['bracket_stop']}"
    )
    assert pos["trailing_last_r"] == pytest.approx(1.0)

    # And it must never lower the stop on subsequent unfavorable ticks.
    bar_pullback = {
        "open": 18_010.0, "high": 18_005.0, "low": 17_995.0, "close": 18_001.0,
    }
    sup._maybe_apply_trailing_stop(bot, pos, bar_pullback)
    assert pos["bracket_stop"] == pytest.approx(18_000.0), (
        "stop must not move adversely after activation"
    )


def test_trailing_stop_disabled_via_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ETA_TRAILING_STOP_ENABLED=false, the helper is a no-op
    even at +5R. This is the operator's kill-switch in case the
    feature behaves badly in production."""
    monkeypatch.setenv("ETA_TRAILING_STOP_ENABLED", "false")

    sup = _make_supervisor(tmp_path)
    bot = _make_bot()
    pos = bot.open_position
    bar = {"open": 18_500.0, "high": 18_600.0, "low": 18_500.0, "close": 18_550.0}

    sup._maybe_apply_trailing_stop(bot, pos, bar)
    assert pos["bracket_stop"] == 17_900.0, "stop must not move when feature disabled"
    assert pos.get("trailing_active") is not True


def test_trailing_stop_short_position_lowers_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SHORT positions: trailing stop must MOVE DOWN with price (never
    up against us). At +1R for a SHORT, stop must move from above entry
    to entry exactly."""
    monkeypatch.setenv("ETA_TRAILING_STOP_ENABLED", "true")
    monkeypatch.setenv("ETA_TRAILING_STOP_ACTIVATE_R", "1.0")

    sup = _make_supervisor(tmp_path)
    bot = _make_bot()
    # Flip to a SHORT layout: entry=18000, stop=18100 (above), target=17800.
    bot.open_position = {
        "side": "SELL",
        "qty": 2.0,
        "entry_price": 18_000.0,
        "entry_ts": "2026-05-06T11:00:00+00:00",
        "signal_id": "alpha_entry",
        "bracket_stop": 18_100.0,
        "bracket_target": 17_800.0,
    }
    pos = bot.open_position
    # Bar with low == 17900 prints +1R for a SHORT (1R = 100 pts).
    bar = {"open": 17_950.0, "high": 17_960.0, "low": 17_900.0, "close": 17_950.0}

    sup._maybe_apply_trailing_stop(bot, pos, bar)

    assert pos["bracket_stop"] == pytest.approx(18_000.0), (
        f"SHORT stop must move down to entry at +1R; got {pos['bracket_stop']}"
    )
    assert pos["trailing_active"] is True


# ---------------------------------------------------------------------------
# 2. Partial profit closes half
# ---------------------------------------------------------------------------


def test_partial_profit_closes_half(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At +1R, half the position should be closed via submit_exit and
    the remaining qty written back to bot.open_position.qty.

    Validated via a stub ``submit_exit`` that captures the call but
    doesn't try to talk to a broker. The runner dict that lands on
    ``bot.open_position`` after the partial fire MUST carry:
      - qty = original_qty * (1 - PARTIAL_PCT)
      - partial_taken = True
    so a second tick at the same R doesn't fire a second partial.
    """
    monkeypatch.setenv("ETA_PARTIAL_PROFIT_ENABLED", "true")
    monkeypatch.setenv("ETA_PARTIAL_PROFIT_R", "1.0")
    monkeypatch.setenv("ETA_PARTIAL_PROFIT_PCT", "0.5")
    # Disable trailing stop so it doesn't interfere with the captured qty.
    monkeypatch.setenv("ETA_TRAILING_STOP_ENABLED", "false")

    sup = _make_supervisor(tmp_path)
    bot = _make_bot()
    initial_qty = bot.open_position["qty"]
    bar = {"open": 18_050.0, "high": 18_100.0, "low": 18_040.0, "close": 18_080.0}

    # Stub the router's submit_exit to: (a) clear bot.open_position
    # like the real one does, (b) return a synthetic FillRecord.
    captured_calls: list[dict[str, Any]] = []

    from eta_engine.scripts.jarvis_strategy_supervisor import FillRecord

    def _fake_submit_exit(*, bot, bar):  # noqa: ANN001 — match real signature
        captured_calls.append({
            "bot_id": bot.bot_id,
            "qty": bot.open_position["qty"],
            "exit_reason": bot.open_position.get("exit_reason"),
        })
        rec = FillRecord(
            bot_id=bot.bot_id,
            signal_id="partial-exit-001",
            side="SELL",
            symbol=bot.symbol,
            qty=bot.open_position["qty"],
            fill_price=18_100.0,
            fill_ts="2026-05-06T12:00:00+00:00",
            paper=True,
            realized_r=1.0,
            realized_pnl=200.0,
            note="partial",
        )
        rec.entry_snapshot = {  # type: ignore[attr-defined]
            "side": bot.open_position["side"],
            "entry_price": bot.open_position["entry_price"],
            "qty": bot.open_position["qty"],
            "bracket_stop": bot.open_position["bracket_stop"],
            "bracket_target": bot.open_position["bracket_target"],
            "signal_id": bot.open_position["signal_id"],
        }
        bot.open_position = None  # mirror real submit_exit's clear
        return rec

    monkeypatch.setattr(sup._router, "submit_exit", _fake_submit_exit)
    # Stub _propagate_close so we don't drag in JARVIS / memory layers.
    monkeypatch.setattr(sup, "_propagate_close", lambda *a, **kw: None)

    sup._maybe_take_partial_profit(bot, bot.open_position, bar)

    # Exactly one submit_exit call, sized to half the original qty.
    assert len(captured_calls) == 1, (
        f"expected 1 partial-exit call, got {len(captured_calls)}"
    )
    assert captured_calls[0]["qty"] == pytest.approx(initial_qty * 0.5), (
        f"partial close qty should be 50% of {initial_qty}; "
        f"got {captured_calls[0]['qty']}"
    )
    assert captured_calls[0]["exit_reason"] == "paper_partial_profit"

    # Runner persists with the remaining qty.
    assert bot.open_position is not None, (
        "bot.open_position must be restored after partial; submit_exit "
        "cleared it but the helper has to bring back the runner dict"
    )
    assert bot.open_position["qty"] == pytest.approx(initial_qty * 0.5)
    assert bot.open_position["partial_taken"] is True
    assert bot.open_position["partial_qty"] == pytest.approx(initial_qty * 0.5)

    # Second invocation at the same R must NOT fire again — partial_taken
    # gates the helper's dedup. Without this, every tick at +1R would
    # close another 50%.
    sup._maybe_take_partial_profit(bot, bot.open_position, bar)
    assert len(captured_calls) == 1, (
        "second tick at same R must not re-fire partial profit"
    )


def test_partial_profit_disabled_via_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ETA_PARTIAL_PROFIT_ENABLED=false → no exit submission even at +5R."""
    monkeypatch.setenv("ETA_PARTIAL_PROFIT_ENABLED", "false")

    sup = _make_supervisor(tmp_path)
    bot = _make_bot()
    bar = {"open": 18_500.0, "high": 18_500.0, "low": 18_400.0, "close": 18_500.0}

    captured: list[Any] = []

    def _fake_submit_exit(*, bot, bar):  # noqa: ANN001
        captured.append(bot.bot_id)
        return None

    monkeypatch.setattr(sup._router, "submit_exit", _fake_submit_exit)
    sup._maybe_take_partial_profit(bot, bot.open_position, bar)

    assert captured == [], "no exit must fire when partial-profit disabled"
    assert bot.open_position["qty"] == 2.0, "qty must not change"


# ---------------------------------------------------------------------------
# 3. Signal aggregation dedups same-bar same-direction
# ---------------------------------------------------------------------------


def test_signal_aggregation_dedups_same_bar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three bots all fire BUY MNQ inside the aggregation window:
    bot_a goes through, bot_b and bot_c are consolidated against
    bot_a's first-mover record.

    The rejected bots receive a ``consolidated_with_<first_bot_id>``
    string back from ``_check_signal_aggregation``; the caller writes
    that to ``bot.last_aggregation_reject_reason`` for heartbeat
    surfacing.
    """
    monkeypatch.setenv("ETA_BOT_AGGREGATION_ENABLED", "true")
    monkeypatch.setenv("ETA_BOT_AGGREGATION_WINDOW_S", "300")

    sup = _make_supervisor(tmp_path)

    # Three bots all targeting MNQ (same symbol). bot_a fires first.
    from eta_engine.scripts.jarvis_strategy_supervisor import BotInstance

    bot_a = BotInstance(
        bot_id="alpha_a", symbol="MNQ", strategy_kind="futures",
        direction="long", cash=5_000.0,
    )
    bot_b = BotInstance(
        bot_id="alpha_b", symbol="MNQ", strategy_kind="futures",
        direction="long", cash=5_000.0,
    )
    bot_c = BotInstance(
        bot_id="alpha_c", symbol="MNQ", strategy_kind="futures",
        direction="long", cash=5_000.0,
    )
    bar = {"open": 18_000.0, "high": 18_010.0, "low": 17_995.0, "close": 18_005.0}

    # First mover: returns None (allowed).
    reason_a = sup._check_signal_aggregation(bot=bot_a, side="BUY", bar=bar)
    assert reason_a is None, "first bot must be allowed through"

    # Second and third bots: rejected, consolidated against alpha_a.
    reason_b = sup._check_signal_aggregation(bot=bot_b, side="BUY", bar=bar)
    reason_c = sup._check_signal_aggregation(bot=bot_c, side="BUY", bar=bar)

    assert reason_b == "consolidated_with_alpha_a", (
        f"bot_b should consolidate against alpha_a, got {reason_b!r}"
    )
    assert reason_c == "consolidated_with_alpha_a", (
        f"bot_c should consolidate against alpha_a, got {reason_c!r}"
    )

    # OPPOSITE direction on the SAME symbol must NOT consolidate (a SELL
    # is a different decision than the BUY that just fired).
    reason_d = sup._check_signal_aggregation(bot=bot_b, side="SELL", bar=bar)
    assert reason_d is None, (
        "same-symbol opposite-direction entry must not consolidate; "
        "got {reason_d!r}"
    )

    # Different symbol must NOT consolidate either.
    bot_e = BotInstance(
        bot_id="eth_strat", symbol="ETH", strategy_kind="crypto",
        direction="long", cash=5_000.0,
    )
    reason_e = sup._check_signal_aggregation(bot=bot_e, side="BUY", bar=bar)
    assert reason_e is None, "different symbol must not consolidate"


def test_signal_aggregation_disabled_via_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ETA_BOT_AGGREGATION_ENABLED=false → all entries pass through."""
    monkeypatch.setenv("ETA_BOT_AGGREGATION_ENABLED", "false")
    sup = _make_supervisor(tmp_path)

    from eta_engine.scripts.jarvis_strategy_supervisor import BotInstance

    bot_a = BotInstance(
        bot_id="alpha_a", symbol="MNQ", strategy_kind="futures",
        direction="long", cash=5_000.0,
    )
    bot_b = BotInstance(
        bot_id="alpha_b", symbol="MNQ", strategy_kind="futures",
        direction="long", cash=5_000.0,
    )
    bar = {"close": 18_000.0}

    assert sup._check_signal_aggregation(bot=bot_a, side="BUY", bar=bar) is None
    assert sup._check_signal_aggregation(bot=bot_b, side="BUY", bar=bar) is None


def test_signal_aggregation_window_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside the aggregation window, sibling bots can fire freely.

    We force the cache's ``fired_at`` backwards in time to simulate a
    long gap, then verify the second bot is allowed through.
    """
    monkeypatch.setenv("ETA_BOT_AGGREGATION_ENABLED", "true")
    monkeypatch.setenv("ETA_BOT_AGGREGATION_WINDOW_S", "60")

    sup = _make_supervisor(tmp_path)

    from eta_engine.scripts.jarvis_strategy_supervisor import BotInstance

    bot_a = BotInstance(
        bot_id="alpha_a", symbol="MNQ", strategy_kind="futures",
        direction="long", cash=5_000.0,
    )
    bot_b = BotInstance(
        bot_id="alpha_b", symbol="MNQ", strategy_kind="futures",
        direction="long", cash=5_000.0,
    )
    bar = {"close": 18_000.0}

    assert sup._check_signal_aggregation(bot=bot_a, side="BUY", bar=bar) is None

    # Rewind the cache entry to 999s ago (way past the 60s window).
    cache_key = ("MNQ", "BUY")
    sup._aggregation_cache[cache_key]["fired_at"] -= 999.0

    reason = sup._check_signal_aggregation(bot=bot_b, side="BUY", bar=bar)
    assert reason is None, (
        f"sibling outside window must be allowed, got {reason!r}"
    )
