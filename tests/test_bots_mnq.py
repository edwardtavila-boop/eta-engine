"""
EVOLUTIONARY TRADING ALGO  //  tests.test_bots_mnq
======================================
Exercise the wired MNQ bot:

* 4 setups fire against synthetic bars
* signal → router sizing uses risk-per-trade + stop_distance meta
* trailing-stop exit after peak-drawdown on an open position
* router failure doesn't raise out of on_signal
* stop() clears trailing peak state
"""

from __future__ import annotations

import pytest

from eta_engine.bots.base_bot import Position, RegimeType, Signal, SignalType
from eta_engine.bots.mnq.bot import MnqBot
from eta_engine.venues.base import OrderRequest, OrderResult, OrderStatus, Side


class _FakeRouter:
    """Routes calls to an ordered list of pre-canned OrderResult (or raises)."""

    def __init__(self, results: list[OrderResult | Exception]) -> None:
        self._results = list(results)
        self.calls: list[OrderRequest] = []

    async def place_with_failover(self, req: OrderRequest) -> OrderResult:
        self.calls.append(req)
        if not self._results:
            return OrderResult(order_id="FAKE-EMPTY", status=OrderStatus.FILLED, filled_qty=req.qty)
        nxt = self._results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


# --------------------------------------------------------------------------- #
# Setup firing: bars crafted to trigger each of the 4 setups in turn
# --------------------------------------------------------------------------- #
def test_orb_breakout_long_fires_with_volume_confirmation() -> None:
    bot = MnqBot()
    bar = {
        "open": 25_000, "high": 25_050, "low": 24_990, "close": 25_050,
        "volume": 5000, "avg_volume": 1000,
        "orb_high": 25_040, "orb_low": 24_900,
        "atr_14": 10.0, "adx_14": 35.0,  # trending
    }
    sig = bot.orb_breakout(bar, RegimeType.TRENDING, None)
    assert sig is not None
    assert sig.type is SignalType.LONG
    assert sig.meta["setup"] == "orb_breakout"
    assert sig.meta["stop_distance"] == 15.0  # 10 * 1.5


def test_orb_breakout_no_volume_no_signal() -> None:
    bot = MnqBot()
    bar = {
        "close": 25_050, "volume": 500, "avg_volume": 1000,
        "orb_high": 25_040, "orb_low": 24_900, "atr_14": 10.0,
    }
    assert bot.orb_breakout(bar, RegimeType.TRENDING, None) is None


def test_ema_pullback_requires_trending_regime() -> None:
    bot = MnqBot()
    # In RANGING regime — should not fire even with perfect EMA touch
    bar = {"open": 25_000, "close": 25_010, "ema_21": 25_010, "atr_14": 8.0}
    assert bot.ema_pullback(bar, RegimeType.RANGING, None) is None
    # In TRENDING regime with bullish candle touching EMA
    bar2 = {"open": 25_000, "close": 25_011, "ema_21": 25_010, "atr_14": 8.0}
    sig = bot.ema_pullback(bar2, RegimeType.TRENDING, None)
    assert sig is not None
    assert sig.type is SignalType.LONG


def test_sweep_reclaim_fires_on_confirmed_reclaim() -> None:
    from eta_engine.bots.base_bot import SweepResult
    bot = MnqBot()
    bar = {"close": 25_100, "atr_14": 8.0}
    sweep = SweepResult(swept=True, direction=SignalType.LONG, level=25_080, reclaim_confirmed=True)
    sig = bot.sweep_reclaim(bar, RegimeType.TRENDING, sweep)
    assert sig is not None
    assert sig.confidence == 8.0
    assert sig.meta["sweep_level"] == 25_080


def test_mean_reversion_fires_on_zscore_only_in_ranging() -> None:
    bot = MnqBot()
    bar = {"close": 25_000, "vwap": 25_030, "atr_14": 10.0}  # -3 z
    assert bot.mean_reversion(bar, RegimeType.TRENDING, None) is None
    sig = bot.mean_reversion(bar, RegimeType.RANGING, None)
    assert sig is not None
    assert sig.type is SignalType.LONG


# --------------------------------------------------------------------------- #
# Signal sizing
# --------------------------------------------------------------------------- #
def test_size_from_signal_uses_stop_distance_meta() -> None:
    bot = MnqBot()
    # equity=$5000, risk=1% -> $50 risk budget. stop_distance=5 pts = $10/contract
    sig = Signal(type=SignalType.LONG, symbol="MNQ", price=25_000,
                 confidence=7.0, meta={"stop_distance": 5.0})
    qty = bot._size_from_signal(sig)
    # $50 / ($10/contract) = 5 contracts
    assert qty == 5.0


def test_size_from_signal_zero_when_stop_dist_zero() -> None:
    bot = MnqBot()
    sig = Signal(type=SignalType.LONG, symbol="MNQ", price=25_000,
                 meta={"stop_distance": 0.0})
    assert bot._size_from_signal(sig) == 0.0


def test_size_from_signal_preserves_explicit_size() -> None:
    bot = MnqBot()
    sig = Signal(type=SignalType.LONG, symbol="MNQ", price=25_000, size=3.0)
    assert bot._size_from_signal(sig) == 3.0


# --------------------------------------------------------------------------- #
# on_signal → router wiring
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_on_signal_routes_to_router_with_buy_side() -> None:
    results = [OrderResult(order_id="T-1", status=OrderStatus.FILLED, filled_qty=5.0, avg_price=25_001)]
    router = _FakeRouter(results)
    bot = MnqBot(router=router, tradovate_symbol="MNQH6")
    sig = Signal(type=SignalType.LONG, symbol="MNQ", price=25_000,
                 confidence=7.0, meta={"stop_distance": 5.0})
    res = await bot.on_signal(sig)
    assert res is not None
    assert res.status is OrderStatus.FILLED
    assert len(router.calls) == 1
    req = router.calls[0]
    assert req.symbol == "MNQH6"
    assert req.side is Side.BUY
    assert req.qty == 5.0
    assert req.reduce_only is False


@pytest.mark.asyncio
async def test_on_signal_close_short_uses_reduce_only_buy() -> None:
    router = _FakeRouter([OrderResult(order_id="T-2", status=OrderStatus.FILLED, filled_qty=2.0)])
    bot = MnqBot(router=router)
    sig = Signal(type=SignalType.CLOSE_SHORT, symbol="MNQ", price=25_100,
                 size=2.0, meta={})
    await bot.on_signal(sig)
    req = router.calls[0]
    assert req.side is Side.BUY
    assert req.reduce_only is True


@pytest.mark.asyncio
async def test_on_signal_no_router_is_noop() -> None:
    bot = MnqBot()
    sig = Signal(type=SignalType.LONG, symbol="MNQ", price=25_000, size=1.0)
    res = await bot.on_signal(sig)
    assert res is None


@pytest.mark.asyncio
async def test_on_signal_router_exception_returns_none() -> None:
    router = _FakeRouter([RuntimeError("venue down")])
    bot = MnqBot(router=router)
    sig = Signal(type=SignalType.LONG, symbol="MNQ", price=25_000, size=1.0)
    res = await bot.on_signal(sig)
    assert res is None


@pytest.mark.asyncio
async def test_on_signal_skips_when_qty_zero() -> None:
    router = _FakeRouter([])
    bot = MnqBot(router=router)
    # stop_distance=0 → qty=0 → skipped before router call
    sig = Signal(type=SignalType.LONG, symbol="MNQ", price=25_000,
                 meta={"stop_distance": 0.0})
    res = await bot.on_signal(sig)
    assert res is None
    assert router.calls == []


# --------------------------------------------------------------------------- #
# Trailing-stop exit
# --------------------------------------------------------------------------- #
def test_trailing_stop_exits_after_peak_drawdown() -> None:
    bot = MnqBot()
    # equity=5000, risk_per_trade=1% → 1R = $50
    pos = Position(symbol="MNQ", side="LONG", entry_price=25_000, size=1.0, unrealized_pnl=60.0)
    # First call: peak=60 > 50 (1R hit) → trail engages
    assert bot.evaluate_exit(pos) is False
    # PnL retraces by >=1R off peak (60-50=10 remaining) → exit
    pos.unrealized_pnl = 5.0
    assert bot.evaluate_exit(pos) is True


def test_trailing_stop_does_not_engage_below_1r_profit() -> None:
    bot = MnqBot()
    pos = Position(symbol="MNQ", side="LONG", entry_price=25_000, size=1.0, unrealized_pnl=30.0)
    # 30 < 50 (1R) so trail inactive — no exit yet
    assert bot.evaluate_exit(pos) is False


def test_evaluate_exit_hard_stop() -> None:
    bot = MnqBot()
    pos = Position(symbol="MNQ", side="LONG", entry_price=25_000, size=1.0, unrealized_pnl=-60.0)
    assert bot.evaluate_exit(pos) is True


def test_evaluate_exit_2r_target() -> None:
    bot = MnqBot()
    # 2R = 2 * 25000 * 1 * 0.01 = 500
    pos = Position(symbol="MNQ", side="LONG", entry_price=25_000, size=1.0, unrealized_pnl=501.0)
    assert bot.evaluate_exit(pos) is True


# --------------------------------------------------------------------------- #
# Lifecycle + session levels
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_start_stop_smoke() -> None:
    bot = MnqBot(session_levels=[25_000, 24_900, 25_100])
    await bot.start()
    assert bot._liquidity_levels == [25_000, 24_900, 25_100]
    # Seed trailing peak then stop → clears state
    bot._trailing_peak["MNQ@25000.0000"] = 42.0
    await bot.stop()
    assert bot._trailing_peak == {}


def test_load_session_levels_replaces() -> None:
    bot = MnqBot(session_levels=[1.0])
    bot.load_session_levels([10.0, 20.0])
    assert bot._liquidity_levels == [10.0, 20.0]


# --------------------------------------------------------------------------- #
# End-to-end: on_bar routes ORB long to the router
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_on_bar_orb_long_routes_to_router() -> None:
    results = [OrderResult(order_id="E2E-1", status=OrderStatus.FILLED, filled_qty=5.0, avg_price=25_050)]
    router = _FakeRouter(results)
    bot = MnqBot(router=router)
    bar = {
        "open": 25_000, "high": 25_050, "low": 24_990, "close": 25_050,
        "volume": 5000, "avg_volume": 1000,
        "orb_high": 25_040, "orb_low": 24_900,
        "atr_14": 10.0, "adx_14": 35.0,
    }
    await bot.on_bar(bar)
    assert len(router.calls) == 1
    assert router.calls[0].side is Side.BUY
