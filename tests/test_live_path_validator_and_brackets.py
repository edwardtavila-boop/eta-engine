"""Regression tests for the four STOP-LIVE-MONEY live-path patches.

Locks in:
1. OrderRequest carries stop_price + target_price + bot_id
2. The IBKR venue rejects naked entries (no bracket attached)
3. Bracket entries call placeOrder THREE TIMES (parent + sl + tp)
4. Reduce-only exits skip the bracket requirement (single placeOrder call)
5. MnqLiveSupervisor.reconcile_with_broker seeds local state from broker
6. reconcile_with_broker detects divergence on subsequent calls
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ── OrderRequest schema ───────────────────────────────────────────


def test_order_request_carries_bracket_fields():
    from eta_engine.venues.base import OrderRequest, Side
    req = OrderRequest(
        symbol="MNQ1", side=Side.BUY, qty=2.0,
        stop_price=27000.0, target_price=27050.0, bot_id="vp_mnq",
    )
    assert req.qty == 2.0
    assert req.stop_price == 27000.0
    assert req.target_price == 27050.0
    assert req.bot_id == "vp_mnq"


def test_order_request_bracket_fields_optional():
    from eta_engine.venues.base import OrderRequest, Side
    req = OrderRequest(symbol="MNQ1", side=Side.BUY, qty=1.0)
    assert req.stop_price is None
    assert req.target_price is None
    assert req.bot_id is None


# ── Bracket-or-reject in venue ────────────────────────────────────


@pytest.mark.asyncio
async def test_venue_rejects_naked_entry():
    """Entry order without stop_price + target_price MUST be rejected."""
    import os
    os.environ["ETA_LIVE_TRADING_ENABLED"] = "1"
    os.environ["ETA_FLEET_RISK_LIMIT"] = "100000"
    os.environ["ETA_POSITION_CAP"] = "5"

    from eta_engine.venues.base import OrderRequest, Side
    from eta_engine.venues.ibkr_live import LiveIbkrVenue

    venue = LiveIbkrVenue()
    # Stub the connect path so the test doesn't reach for real TWS
    venue._ensure_connected = AsyncMock(return_value=True)
    venue._ib = MagicMock()
    venue._ib.placeOrder = MagicMock(side_effect=AssertionError(
        "naked entry should never reach placeOrder",
    ))

    req = OrderRequest(
        symbol="MNQ1", side=Side.BUY, qty=1.0,
        stop_price=None, target_price=None,  # no bracket — must be rejected
        reduce_only=False,
    )
    result = await venue.place_order(req)
    assert result.status.value == "REJECTED"
    assert "missing bracket" in result.raw["reason"]


@pytest.mark.asyncio
async def test_venue_accepts_bracket_entry():
    """Entry with full bracket should construct a 3-leg OCO via bracketOrder."""
    import os
    os.environ["ETA_LIVE_TRADING_ENABLED"] = "1"
    os.environ["ETA_FLEET_RISK_LIMIT"] = "100000"
    os.environ["ETA_POSITION_CAP"] = "5"

    from eta_engine.venues.base import OrderRequest, Side
    from eta_engine.venues.ibkr_live import LiveIbkrVenue

    venue = LiveIbkrVenue()
    venue._ensure_connected = AsyncMock(return_value=True)
    venue._ib = MagicMock()

    # bracketOrder returns 3 stub orders
    parent_trade = MagicMock()
    parent_trade.order.orderId = 1001
    sl_trade = MagicMock()
    sl_trade.order.orderId = 1002
    tp_trade = MagicMock()
    tp_trade.order.orderId = 1003
    venue._ib.bracketOrder = MagicMock(return_value=[
        MagicMock(), MagicMock(), MagicMock(),
    ])
    place_results = [parent_trade, tp_trade, sl_trade]
    venue._ib.placeOrder = MagicMock(side_effect=place_results)

    req = OrderRequest(
        symbol="MNQ1", side=Side.BUY, qty=2.0,
        stop_price=26900.0, target_price=27100.0,
        reduce_only=False,
    )
    result = await venue.place_order(req)
    assert result.status.value == "OPEN", result.raw
    # Must have called placeOrder THREE times — one per bracket leg
    assert venue._ib.placeOrder.call_count == 3
    venue._ib.bracketOrder.assert_called_once()


@pytest.mark.asyncio
async def test_venue_reduce_only_exit_skips_bracket():
    """Reduce-only exits (closes) bypass the bracket requirement —
    they fire a single market order."""
    import os
    os.environ["ETA_LIVE_TRADING_ENABLED"] = "1"
    os.environ["ETA_FLEET_RISK_LIMIT"] = "100000"
    os.environ["ETA_POSITION_CAP"] = "5"

    from eta_engine.venues.base import OrderRequest, Side
    from eta_engine.venues.ibkr_live import LiveIbkrVenue

    venue = LiveIbkrVenue()
    venue._ensure_connected = AsyncMock(return_value=True)
    venue._ib = MagicMock()
    exit_trade = MagicMock()
    exit_trade.order.orderId = 2001
    venue._ib.placeOrder = MagicMock(return_value=exit_trade)

    req = OrderRequest(
        symbol="MNQ1", side=Side.SELL, qty=1.0,
        reduce_only=True,
        stop_price=None, target_price=None,  # exits don't need a bracket
    )
    result = await venue.place_order(req)
    assert result.status.value == "OPEN", result.raw
    # Single placeOrder call, NOT bracketOrder
    assert venue._ib.placeOrder.call_count == 1
    assert not hasattr(venue._ib, "bracketOrder") or not venue._ib.bracketOrder.called


# ── Crypto live-path: post-fill bracket via callback ─────────────


@pytest.mark.asyncio
async def test_venue_crypto_entry_places_post_fill_bracket():
    """Crypto entries MUST: (a) place a market entry, (b) wait for
    fill, (c) place stop + target as standing orders, (d) wire OCO
    fillEvent callbacks. PAXOS rejects bracketOrder, so we manage
    the bracket ourselves."""
    import os
    os.environ["ETA_LIVE_TRADING_ENABLED"] = "1"
    os.environ["ETA_FLEET_RISK_LIMIT"] = "100000"
    os.environ["ETA_POSITION_CAP"] = "5"
    os.environ["ETA_IBKR_CRYPTO"] = "1"  # opt into the live crypto path

    from eta_engine.venues.base import OrderRequest, Side
    from eta_engine.venues.ibkr_live import LiveIbkrVenue

    venue = LiveIbkrVenue()
    venue._ensure_connected = AsyncMock(return_value=True)
    venue._ib = MagicMock()

    # Entry trade — pretend it fills immediately
    entry_trade = MagicMock()
    entry_trade.order.orderId = 5001
    entry_trade.orderStatus.status = "Filled"
    entry_trade.orderStatus.avgFillPrice = 95000.0

    # filledEvent must support `+=` (returning self) so the venue can
    # register the OCO canceler. Plain MagicMock auto-creates a new
    # attribute mock on each access, which breaks call-count assertions.
    stop_callbacks: list = []
    stop_trade = MagicMock()
    stop_trade.order.orderId = 5002
    stop_trade.orderStatus.status = "Submitted"
    stop_trade.filledEvent = MagicMock()
    stop_trade.filledEvent.__iadd__ = MagicMock(side_effect=lambda cb: (stop_callbacks.append(cb), stop_trade.filledEvent)[1])

    target_callbacks: list = []
    target_trade = MagicMock()
    target_trade.order.orderId = 5003
    target_trade.orderStatus.status = "Submitted"
    target_trade.filledEvent = MagicMock()
    target_trade.filledEvent.__iadd__ = MagicMock(side_effect=lambda cb: (target_callbacks.append(cb), target_trade.filledEvent)[1])

    venue._ib.placeOrder = MagicMock(side_effect=[entry_trade, stop_trade, target_trade])

    req = OrderRequest(
        symbol="BTC", side=Side.BUY, qty=0.01,
        stop_price=93575.0, target_price=96900.0,
        reduce_only=False,
    )
    result = await venue.place_order(req)

    assert result.status.value == "OPEN", result.raw
    # Three placeOrder calls — entry + stop + target
    assert venue._ib.placeOrder.call_count == 3
    # OCO callbacks registered (one canceler per leg)
    assert len(stop_callbacks) == 1, "stop fillEvent canceler missing"
    assert len(target_callbacks) == 1, "target fillEvent canceler missing"
    # Tracked in the venue's order book under the stop/target suffix
    assert any(k.endswith(":stop") for k in venue._orders)
    assert any(k.endswith(":target") for k in venue._orders)

    # Reset env so subsequent tests don't see the live-crypto opt-in
    os.environ.pop("ETA_IBKR_CRYPTO", None)


@pytest.mark.asyncio
async def test_venue_crypto_oco_callback_cancels_sibling():
    """When the stop fills, the target must auto-cancel — the OCO
    semantics that PAXOS doesn't give us natively."""
    import os
    os.environ["ETA_LIVE_TRADING_ENABLED"] = "1"
    os.environ["ETA_FLEET_RISK_LIMIT"] = "100000"
    os.environ["ETA_POSITION_CAP"] = "5"
    os.environ["ETA_IBKR_CRYPTO"] = "1"

    from eta_engine.venues.base import OrderRequest, Side
    from eta_engine.venues.ibkr_live import LiveIbkrVenue

    venue = LiveIbkrVenue()
    venue._ensure_connected = AsyncMock(return_value=True)
    venue._ib = MagicMock()

    # Capture fill callbacks for replay below
    stop_callbacks: list = []
    target_callbacks: list = []

    entry_trade = MagicMock()
    entry_trade.order.orderId = 7001
    entry_trade.orderStatus.status = "Filled"
    entry_trade.orderStatus.avgFillPrice = 3500.0

    stop_trade = MagicMock()
    stop_trade.order.orderId = 7002
    stop_trade.orderStatus.status = "Submitted"
    stop_trade.filledEvent = MagicMock()
    stop_trade.filledEvent.__iadd__ = MagicMock(side_effect=lambda cb: (stop_callbacks.append(cb), stop_trade.filledEvent)[1])

    target_trade = MagicMock()
    target_trade.order.orderId = 7003
    target_trade.orderStatus.status = "Submitted"
    target_trade.filledEvent = MagicMock()
    target_trade.filledEvent.__iadd__ = MagicMock(side_effect=lambda cb: (target_callbacks.append(cb), target_trade.filledEvent)[1])

    venue._ib.placeOrder = MagicMock(side_effect=[entry_trade, stop_trade, target_trade])
    venue._ib.cancelOrder = MagicMock()

    req = OrderRequest(
        symbol="ETH", side=Side.BUY, qty=0.25,
        stop_price=3447.5, target_price=3570.0,
        reduce_only=False,
    )
    await venue.place_order(req)

    # Now simulate: the stop fills.
    assert len(stop_callbacks) == 1, "stop fillEvent callback not registered"
    stop_callbacks[0]()  # invoke the canceler
    venue._ib.cancelOrder.assert_called_once_with(target_trade.order)

    # Conversely, if target had filled instead, stop would be cancelled.
    venue._ib.cancelOrder.reset_mock()
    assert len(target_callbacks) == 1, "target fillEvent callback not registered"
    target_callbacks[0]()
    venue._ib.cancelOrder.assert_called_once_with(stop_trade.order)

    os.environ.pop("ETA_IBKR_CRYPTO", None)


# ── Supervisor reconcile_with_broker ──────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_reconcile_seeds_local_from_broker():
    """On initial reconcile, broker positions seed local state."""
    from eta_engine.bots.base_bot import BotState, Position

    # Build a fake bot with empty local state and a router whose venue
    # returns a 2-contract MNQ position
    fake_venue = MagicMock()
    fake_venue.get_positions = AsyncMock(return_value=[
        {"symbol": "MNQ1", "position": 2.0},
    ])
    fake_router = MagicMock()
    fake_router._venue = fake_venue
    fake_bot = MagicMock()
    fake_bot._router = fake_router
    fake_bot.state = BotState()
    fake_bot.state.open_positions = []
    fake_bot.state.is_paused = False

    # Build supervisor with stubbed bot — only test reconcile method directly
    from eta_engine.feeds.mnq_live_supervisor import MnqLiveSupervisor
    sup = MnqLiveSupervisor.__new__(MnqLiveSupervisor)
    sup.bot = fake_bot
    sup.state = MagicMock()
    sup.state.last_event = ""
    from datetime import UTC, datetime
    sup.clock = lambda: datetime.now(UTC)

    result = await sup.reconcile_with_broker(initial=True)
    assert result["reconciled"] is True
    assert result["broker"] == {"MNQ1": 2.0}
    assert len(fake_bot.state.open_positions) == 1
    assert fake_bot.state.open_positions[0].symbol == "MNQ1"
    assert fake_bot.state.open_positions[0].size == 2.0
    assert fake_bot.state.open_positions[0].side == "long"


@pytest.mark.asyncio
async def test_supervisor_reconcile_detects_divergence():
    """On subsequent reconcile, broker-vs-local divergence is captured."""
    from eta_engine.bots.base_bot import BotState, Position

    fake_venue = MagicMock()
    fake_venue.get_positions = AsyncMock(return_value=[
        {"symbol": "MNQ1", "position": 1.0},  # broker says 1
    ])
    fake_router = MagicMock()
    fake_router._venue = fake_venue
    fake_bot = MagicMock()
    fake_bot._router = fake_router
    fake_bot.state = BotState()
    # Local thinks we hold 2
    fake_bot.state.open_positions = [
        Position(symbol="MNQ1", side="long", entry_price=27000.0, size=2.0),
    ]
    fake_bot.state.is_paused = False
    fake_bot._circuit_breaker = None
    fake_bot.circuit_breaker = None

    from eta_engine.feeds.mnq_live_supervisor import MnqLiveSupervisor
    sup = MnqLiveSupervisor.__new__(MnqLiveSupervisor)
    sup.bot = fake_bot
    sup.state = MagicMock()
    sup.state.last_event = ""
    from datetime import UTC, datetime
    sup.clock = lambda: datetime.now(UTC)

    result = await sup.reconcile_with_broker(initial=False)
    assert result["divergence"] == {"MNQ1": {"broker": 1.0, "local": 2.0}}


@pytest.mark.asyncio
async def test_supervisor_reconcile_no_venue_returns_skip():
    """If no router/venue is wired, reconcile is a no-op."""
    fake_bot = MagicMock()
    fake_bot._router = None

    from eta_engine.feeds.mnq_live_supervisor import MnqLiveSupervisor
    sup = MnqLiveSupervisor.__new__(MnqLiveSupervisor)
    sup.bot = fake_bot
    sup.state = MagicMock()
    sup.state.last_event = ""

    result = await sup.reconcile_with_broker(initial=True)
    assert result == {"reconciled": False, "reason": "no_venue"}
