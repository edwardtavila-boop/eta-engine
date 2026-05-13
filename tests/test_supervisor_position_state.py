"""Tests for supervisor position-state correctness fixes.

Covers the three P0 audit items:

1. Phantom position guard: bot.open_position must NEVER reflect a
   position the broker did not accept. Both REJECTED status and any
   raised exception during the broker call must clear it and bump the
   per-bot reject counter; a successful OPEN/PARTIAL/FILLED status must
   reset the counter to 0.

2. _propagate_close must receive the original entry-side / entry-price
   from a snapshot taken BEFORE submit_exit clears bot.open_position,
   so the edge_tracker observation uses real entry data — not the exit
   FillRecord side or 0.0 fallback.

3. submit_exit must size against the broker's authoritative position
   when broker qty < supervisor qty (partial-fill drift), and log a
   WARNING describing the divergence.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class _StubVenue:
    """Bare async venue stub.

    Real ``LiveIbkrVenue.place_order`` is ``async`` — production calls
    ``_venue.place_order(_req)`` to build the coroutine, then hands that
    coroutine to ``_run_on_live_ibkr_loop``. Tests patch
    ``_run_on_live_ibkr_loop`` to bypass the loop, so this stub just
    needs to expose an async ``place_order`` that produces a coroutine
    object (its body never actually runs because the patched runner
    returns its own canned ``OrderResult`` and discards the coroutine).
    """

    async def place_order(self, _req):  # noqa: ANN001
        return None


def _close_coro(value) -> None:  # noqa: ANN001
    if inspect.iscoroutine(value):
        value.close()


# --------------------------------------------------------------------- #
# Fix 1: phantom position on broker rejection / exception
# --------------------------------------------------------------------- #


def test_open_position_not_set_when_broker_rejects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """REJECTED broker status → bot.open_position must be None and the
    reject counter must be 1."""
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    from eta_engine.venues.base import OrderResult, OrderStatus

    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "direct_ibkr"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    # Cash > 10x price so the supervisor's pre-cap risk_unit produces
    # qty >= 1.0; cap_qty_to_budget then floors to 1 contract via the
    # paper_futures_floor branch and the broker submit fires.
    bot = BotInstance(
        bot_id="reject_bot",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=500_000.0,
    )

    monkeypatch.setattr(supervisor, "_get_live_ibkr_venue", lambda: _StubVenue())

    def _reject(_coro, **_kw):
        _close_coro(_coro)
        return OrderResult(
            order_id="sig-rej",
            status=OrderStatus.REJECTED,
            raw={"ibkr_order_id": 1, "reason": "test reject"},
        )

    monkeypatch.setattr(
        supervisor,
        "_run_on_live_ibkr_loop",
        _reject,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="sig-rej",
        side="BUY",
        bar={"close": 28250.0, "high": 28260.0, "low": 28240.0, "open": 28245.0},
        size_mult=1.0,
    )

    assert rec is None
    assert bot.open_position is None, "PHANTOM POSITION: bot.open_position retained after broker REJECT"
    assert bot.consecutive_broker_rejects == 1


def test_open_position_cleared_when_broker_raises(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Broker call raising must roll back the optimistically-set position
    and increment the reject counter."""
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "direct_ibkr"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="raise_bot",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=500_000.0,
    )

    monkeypatch.setattr(supervisor, "_get_live_ibkr_venue", lambda: _StubVenue())

    def _raise(_coro, **_kw):
        _close_coro(_coro)
        raise RuntimeError("simulated TWS connection error")

    monkeypatch.setattr(supervisor, "_run_on_live_ibkr_loop", _raise)

    rec = router.submit_entry(
        bot=bot,
        signal_id="sig-raise",
        side="BUY",
        bar={"close": 28250.0, "high": 28260.0, "low": 28240.0, "open": 28245.0},
        size_mult=1.0,
    )

    assert rec is None
    assert bot.open_position is None, "PHANTOM POSITION: bot.open_position retained after broker exception"
    assert bot.consecutive_broker_rejects == 1


def test_consecutive_broker_rejects_counter_increments_and_resets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Multiple rejects in a row increment; a success resets to 0."""
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    from eta_engine.venues.base import OrderResult, OrderStatus

    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "direct_ibkr"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="counter_bot",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=500_000.0,
    )

    monkeypatch.setattr(supervisor, "_get_live_ibkr_venue", lambda: _StubVenue())
    monkeypatch.setattr(supervisor.l2hooks, "pre_trade_check", lambda *_args: True)
    monkeypatch.setattr(supervisor.l2hooks, "record_signal", lambda *_args: None)

    # First two rejects bump the counter.
    reject_result = OrderResult(
        order_id="sig",
        status=OrderStatus.REJECTED,
        raw={"ibkr_order_id": 0, "reason": "test"},
    )

    def _reject_counter(_coro, **_kw):
        _close_coro(_coro)
        return reject_result

    monkeypatch.setattr(
        supervisor,
        "_run_on_live_ibkr_loop",
        _reject_counter,
    )

    bar = {"close": 28250.0, "high": 28260.0, "low": 28240.0, "open": 28245.0}
    router.submit_entry(
        bot=bot,
        signal_id="sig-r1",
        side="BUY",
        bar=bar,
        size_mult=1.0,
    )
    assert bot.consecutive_broker_rejects == 1
    router.submit_entry(
        bot=bot,
        signal_id="sig-r2",
        side="BUY",
        bar=bar,
        size_mult=1.0,
    )
    assert bot.consecutive_broker_rejects == 2

    # Now a success — counter must reset.
    open_result = OrderResult(
        order_id="sig",
        status=OrderStatus.OPEN,
        raw={"ibkr_order_id": 99, "reason": ""},
    )

    def _open_counter(_coro, **_kw):
        _close_coro(_coro)
        return open_result

    monkeypatch.setattr(
        supervisor,
        "_run_on_live_ibkr_loop",
        _open_counter,
    )
    rec = router.submit_entry(
        bot=bot,
        signal_id="sig-ok",
        side="BUY",
        bar=bar,
        size_mult=1.0,
    )
    assert rec is not None
    assert bot.open_position is not None
    assert bot.consecutive_broker_rejects == 0


# --------------------------------------------------------------------- #
# Fix 2: _propagate_close must receive snapshot of entry state
# --------------------------------------------------------------------- #


def test_propagate_close_receives_correct_entry_price(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """edge_tracker.observe must see the ORIGINAL entry_price/entry_side
    snapshotted before submit_exit cleared bot.open_position. Previously
    bot.open_position was None at propagate time and the fallback was
    rec.fill_price (the exit price) and the inverted close-side."""
    from collections import deque

    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        JarvisStrategySupervisor,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_sim"
    sup = JarvisStrategySupervisor(cfg=cfg)
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    # Cash large enough that the supervisor's risk_unit/price math
    # produces qty >= 1.0 and the per-bot futures budget cap is also
    # large enough to keep at least 1 contract through cap_qty_to_budget.
    monkeypatch.setenv("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", "1000000")
    monkeypatch.setenv("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", "10000000")
    bot = BotInstance(
        bot_id="propagate_bot",
        symbol="MNQ",
        strategy_kind="x",
        direction="long",
        cash=500_000.0,
    )
    # Synthesize >= 15 bars so the direct edge-tracker block engages.
    base = 28_000.0
    bot.sage_bars = deque(
        [
            {"open": base + i, "high": base + i + 5, "low": base + i - 5, "close": base + i + 1, "volume": 1_000}
            for i in range(20)
        ],
        maxlen=200,
    )
    # Open a paper position; exit at a clearly different price so we
    # can detect a fallback to rec.fill_price.
    router.submit_entry(
        bot=bot,
        signal_id="sig-prop",
        side="BUY",
        bar={"close": 28_100.0, "high": 28_120.0, "low": 28_080.0, "open": 28_090.0},
        size_mult=1.0,
    )
    assert bot.open_position is not None
    expected_entry_price = bot.open_position["entry_price"]
    expected_entry_side = bot.open_position["side"]

    # Capture the MarketContext that consult_sage receives.
    captured: dict = {}

    def _fake_consult_sage(ctx, **_kw):  # noqa: ANN001
        captured["entry_price"] = ctx.entry_price
        captured["entry_side"] = ctx.side

        # Return a no-op report so observe() runs.
        class _Bias:
            value = "neutral"

        class _V:
            bias = _Bias()

        class _R:
            per_school = {"trend": _V()}

        return _R()

    monkeypatch.setattr(
        "eta_engine.brain.jarvis_v3.sage.consult_sage",
        _fake_consult_sage,
    )

    rec = router.submit_exit(
        bot=bot,
        bar={"close": 29_000.0, "high": 29_010.0, "low": 28_990.0, "open": 28_995.0},
    )
    assert rec is not None
    # bot.open_position is now None — that's the whole point of the fix.
    # _maybe_exit calls _propagate_close with the snapshot attached to rec
    # by submit_exit; replicate that flow here.
    assert bot.open_position is None
    snapshot = getattr(rec, "entry_snapshot", None)
    assert snapshot is not None, (
        "submit_exit must attach entry_snapshot to the FillRecord so "
        "_propagate_close has the original entry-side / entry-price"
    )
    sup._propagate_close(bot, rec, entry_snapshot=snapshot)

    # If snapshot is wrong, ctx.entry_price would be ~29000 (rec.fill_price)
    # instead of ~28100 (the original entry).
    assert "entry_price" in captured, "consult_sage was never called"
    assert abs(captured["entry_price"] - expected_entry_price) < 1.0, (
        f"entry_price fallback hit: got {captured['entry_price']!r}, expected {expected_entry_price!r}"
    )
    expected_dir = "long" if expected_entry_side == "BUY" else "short"
    assert captured["entry_side"] == expected_dir


# --------------------------------------------------------------------- #
# Fix 3: submit_exit prefers broker-reported qty over supervisor belief
# --------------------------------------------------------------------- #


def test_submit_exit_uses_broker_qty_when_smaller_than_supervisor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """If broker reports a smaller qty than supervisor's belief, submit_exit
    must size against the broker's number to avoid shipping an oversized
    exit (would either be rejected or flip the position)."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="qty_bot",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=50_000.0,
    )
    bot.open_position = {
        "side": "BUY",
        "qty": 1.0,
        "entry_price": 95_000.0,
        "entry_ts": "2026-05-05T12:00:00+00:00",
        "signal_id": "sig-qty",
        "bracket_stop": 94_000.0,
        "bracket_target": 97_000.0,
    }

    # Stub the broker-qty helper to return 0.5 (broker holds half what
    # the supervisor thinks).
    monkeypatch.setattr(
        ExecutionRouter,
        "_get_broker_position_qty",
        lambda self, b: 0.5,
    )

    rec = router.submit_exit(
        bot=bot,
        bar={"close": 96_000.0, "high": 96_010.0, "low": 95_990.0, "open": 95_995.0},
    )
    assert rec is not None
    assert rec.qty == 0.5, f"submit_exit shipped {rec.qty!r}; should have used broker's 0.5"


def test_submit_exit_logs_warning_on_qty_divergence(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    """Divergence between broker qty and supervisor qty must be logged at
    WARNING with both numbers visible to the operator."""
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="warn_bot",
        symbol="BTC",
        strategy_kind="x",
        direction="long",
        cash=50_000.0,
    )
    bot.open_position = {
        "side": "BUY",
        "qty": 1.0,
        "entry_price": 95_000.0,
        "entry_ts": "2026-05-05T12:00:00+00:00",
        "signal_id": "sig-warn",
        "bracket_stop": 94_000.0,
        "bracket_target": 97_000.0,
    }

    monkeypatch.setattr(
        ExecutionRouter,
        "_get_broker_position_qty",
        lambda self, b: 0.25,
    )

    with caplog.at_level(logging.WARNING):
        router.submit_exit(
            bot=bot,
            bar={"close": 96_000.0, "high": 96_010.0, "low": 95_990.0, "open": 95_995.0},
        )

    # Look for both numbers in any WARNING-level record.
    found = False
    for r in caplog.records:
        if r.levelno >= logging.WARNING:
            msg = r.getMessage()
            if "0.25" in msg and "1.0" in msg and "warn_bot" in msg:
                found = True
                break
    assert found, (
        "expected a WARNING-level log mentioning broker qty 0.25, "
        "supervisor qty 1.0, and bot_id warn_bot; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
