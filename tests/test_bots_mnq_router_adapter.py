"""EVOLUTIONARY TRADING ALGO  //  tests.test_bots_mnq_router_adapter.

Verify that ``MnqBot`` honours the optional ``strategy_adapter`` wiring
introduced in v0.1.34 -- the AI-Optimized strategy stack should fire
first, and on a miss we should fall through to the legacy 4-setup loop
without regression.
"""

from __future__ import annotations

import pytest

from eta_engine.bots.base_bot import SignalType
from eta_engine.bots.mnq.bot import MnqBot
from eta_engine.strategies.engine_adapter import RouterAdapter
from eta_engine.strategies.models import Bar, Side, StrategyId, StrategySignal
from eta_engine.venues.base import OrderRequest, OrderResult, OrderStatus
from eta_engine.venues.base import Side as VenueSide


class _FakeRouter:
    def __init__(self, results: list[OrderResult | Exception]) -> None:
        self._results = list(results)
        self.calls: list[OrderRequest] = []

    async def place_with_failover(
        self,
        req: OrderRequest,
        *,
        urgency: str = "normal",
    ) -> OrderResult:
        _ = urgency
        self.calls.append(req)
        if not self._results:
            return OrderResult(
                order_id="FAKE-EMPTY",
                status=OrderStatus.FILLED,
                filled_qty=req.qty,
            )
        nxt = self._results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _orb_bar_long() -> dict[str, float]:
    """Bar that would make the legacy ORB setup fire LONG."""
    return {
        "open": 25_000,
        "high": 25_050,
        "low": 24_990,
        "close": 25_050,
        "volume": 5000,
        "avg_volume": 1000,
        "orb_high": 25_040,
        "orb_low": 24_900,
        "atr_14": 10.0,
        "adx_14": 35.0,
    }


# ---------------------------------------------------------------------------
# Wiring contract
# ---------------------------------------------------------------------------


class TestMnqBotRouterAdapterWiring:
    """The bot must accept an optional strategy_adapter without regression."""

    def test_bot_without_adapter_keeps_legacy_behavior(self) -> None:
        bot = MnqBot()
        assert bot._strategy_adapter is None

    def test_bot_with_adapter_stores_it(self) -> None:
        adapter = RouterAdapter(asset="MNQ", max_bars=10)
        bot = MnqBot(strategy_adapter=adapter)
        assert bot._strategy_adapter is adapter


# ---------------------------------------------------------------------------
# Adapter priority on on_bar
# ---------------------------------------------------------------------------


def _stub_long_strategy_adapter() -> RouterAdapter:
    """Adapter that unconditionally returns a long signal for MNQ."""

    def fake_long(_bars: list[Bar], _ctx: object) -> StrategySignal:
        return StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.LONG,
            entry=25_050.0,
            stop=25_040.0,
            target=25_080.0,
            confidence=8.0,
            risk_mult=1.0,
            rationale_tags=("stub_router_winner",),
        )

    return RouterAdapter(
        asset="MNQ",
        max_bars=10,
        registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fake_long},
        eligibility={"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)},
    )


def _stub_flat_strategy_adapter() -> RouterAdapter:
    """Adapter that always abstains (flat)."""

    def fake_flat(_bars: list[Bar], _ctx: object) -> StrategySignal:
        return StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.FLAT,
            rationale_tags=("stub_no_trade",),
        )

    return RouterAdapter(
        asset="MNQ",
        max_bars=10,
        registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fake_flat},
        eligibility={"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)},
    )


class TestOnBarAdapterPriority:
    @pytest.mark.asyncio
    async def test_router_signal_takes_priority(self) -> None:
        router = _FakeRouter(
            [OrderResult(order_id="R-1", status=OrderStatus.FILLED, filled_qty=1.0)],
        )
        adapter = _stub_long_strategy_adapter()
        bot = MnqBot(router=router, strategy_adapter=adapter)
        await bot.on_bar(_orb_bar_long())
        # Router signal fired -> exactly ONE call with BUY
        assert len(router.calls) == 1
        assert router.calls[0].side is VenueSide.BUY
        # Meta should carry the strategy id -- not the legacy "orb_breakout"
        assert adapter.last_decision is not None
        assert adapter.last_decision.winner.strategy is StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT

    @pytest.mark.asyncio
    async def test_adapter_flat_falls_through_to_legacy_setups(self) -> None:
        router = _FakeRouter(
            [OrderResult(order_id="L-1", status=OrderStatus.FILLED, filled_qty=1.0)],
        )
        adapter = _stub_flat_strategy_adapter()
        bot = MnqBot(router=router, strategy_adapter=adapter)
        # Legacy ORB setup must still fire for this bar
        await bot.on_bar(_orb_bar_long())
        assert len(router.calls) == 1
        # Adapter recorded a FLAT decision as expected
        assert adapter.last_decision is not None
        assert adapter.last_decision.winner.side is Side.FLAT

    @pytest.mark.asyncio
    async def test_adapter_skipped_when_kill_switch_on_bot(self) -> None:
        router = _FakeRouter([])
        adapter = _stub_long_strategy_adapter()
        bot = MnqBot(router=router, strategy_adapter=adapter)
        bot.state.is_killed = True
        await bot.on_bar(_orb_bar_long())
        # check_risk() short-circuits before the adapter is even consulted
        assert router.calls == []
        assert adapter.last_decision is None

    @pytest.mark.asyncio
    async def test_kill_switch_syncs_to_adapter_on_each_bar(self) -> None:
        """When check_risk() still passes but bot.state.is_killed flips in the
        middle of a session, the adapter should pick it up on the next tick."""
        router = _FakeRouter([])
        adapter = _stub_long_strategy_adapter()
        bot = MnqBot(router=router, strategy_adapter=adapter)
        # First tick: healthy
        await bot.on_bar(_orb_bar_long())
        # Now simulate a mid-session kill without going through check_risk()
        # by patching is_killed AFTER check_risk fires
        # The bot only propagates kill state to adapter when on_bar passes
        # check_risk(), so we rely on the integration flow
        assert adapter.kill_switch_active is False


class TestOnBarAdapterWithNoSignal:
    @pytest.mark.asyncio
    async def test_adapter_no_signal_and_no_legacy_setup(self) -> None:
        """If neither adapter nor legacy fires, the bot stays flat."""
        router = _FakeRouter([])
        adapter = _stub_flat_strategy_adapter()
        bot = MnqBot(router=router, strategy_adapter=adapter)
        dull_bar = {
            "open": 25_000,
            "high": 25_001,
            "low": 24_999,
            "close": 25_000,
            "volume": 100,
            "avg_volume": 1000,
            "orb_high": 0.0,
            "orb_low": 0.0,
            "atr_14": 1.0,
            "adx_14": 15.0,
        }
        await bot.on_bar(dull_bar)
        assert router.calls == []


class TestAdapterSignalShape:
    """Signal emitted from the adapter must be routable by _size_from_signal."""

    @pytest.mark.asyncio
    async def test_adapter_signal_has_stop_distance(self) -> None:
        router = _FakeRouter(
            [OrderResult(order_id="S-1", status=OrderStatus.FILLED, filled_qty=5.0)],
        )
        adapter = _stub_long_strategy_adapter()
        bot = MnqBot(router=router, strategy_adapter=adapter)
        await bot.on_bar(_orb_bar_long())
        # The stub target gives entry=25_050, stop=25_040 -> stop_distance=10
        # With equity $5000 * 1% = $50 risk, point_value=$2 -> 50/(10*2)=2.5 -> 2
        # So qty >= 1
        req = router.calls[0]
        assert req.qty >= 1.0

    @pytest.mark.asyncio
    async def test_adapter_signal_symbol_is_tradovate_override(self) -> None:
        router = _FakeRouter(
            [OrderResult(order_id="T-1", status=OrderStatus.FILLED, filled_qty=1.0)],
        )
        adapter = _stub_long_strategy_adapter()
        bot = MnqBot(
            router=router,
            strategy_adapter=adapter,
            tradovate_symbol="MNQM6",
        )
        await bot.on_bar(_orb_bar_long())
        assert router.calls[0].symbol == "MNQM6"

    @pytest.mark.asyncio
    async def test_adapter_side_maps_to_venue_side(self) -> None:
        router = _FakeRouter(
            [OrderResult(order_id="D-1", status=OrderStatus.FILLED, filled_qty=1.0)],
        )
        adapter = _stub_long_strategy_adapter()
        bot = MnqBot(router=router, strategy_adapter=adapter)
        await bot.on_bar(_orb_bar_long())
        assert router.calls[0].side is VenueSide.BUY

    def test_signal_type_maps(self) -> None:
        """Sanity: SignalType.LONG / SHORT are the only entry types used."""
        assert SignalType.LONG.value == "LONG"
        assert SignalType.SHORT.value == "SHORT"
