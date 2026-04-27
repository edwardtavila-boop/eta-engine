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

    async def place_with_failover(
        self,
        req: OrderRequest,
        *,
        urgency: str = "normal",
    ) -> OrderResult:
        _ = urgency
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
        "open": 25_000,
        "high": 25_050,
        "low": 24_990,
        "close": 25_050,
        "volume": 5000,
        "avg_volume": 1000,
        "orb_high": 25_040,
        "orb_low": 24_900,
        "atr_14": 10.0,
        "adx_14": 35.0,  # trending
    }
    sig = bot.orb_breakout(bar, RegimeType.TRENDING, None)
    assert sig is not None
    assert sig.type is SignalType.LONG
    assert sig.meta["setup"] == "orb_breakout"
    assert sig.meta["stop_distance"] == 15.0  # 10 * 1.5


def test_orb_breakout_no_volume_no_signal() -> None:
    bot = MnqBot()
    bar = {
        "close": 25_050,
        "volume": 500,
        "avg_volume": 1000,
        "orb_high": 25_040,
        "orb_low": 24_900,
        "atr_14": 10.0,
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
    sig = Signal(type=SignalType.LONG, symbol="MNQ", price=25_000, confidence=7.0, meta={"stop_distance": 5.0})
    qty = bot._size_from_signal(sig)
    # $50 / ($10/contract) = 5 contracts
    assert qty == 5.0


def test_size_from_signal_zero_when_stop_dist_zero() -> None:
    bot = MnqBot()
    sig = Signal(type=SignalType.LONG, symbol="MNQ", price=25_000, meta={"stop_distance": 0.0})
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
    sig = Signal(type=SignalType.LONG, symbol="MNQ", price=25_000, confidence=7.0, meta={"stop_distance": 5.0})
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
    sig = Signal(type=SignalType.CLOSE_SHORT, symbol="MNQ", price=25_100, size=2.0, meta={})
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
    sig = Signal(type=SignalType.LONG, symbol="MNQ", price=25_000, meta={"stop_distance": 0.0})
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
    await bot.on_bar(bar)
    assert len(router.calls) == 1
    assert router.calls[0].side is Side.BUY


# --------------------------------------------------------------------------- #
# JARVIS takeover integration
# --------------------------------------------------------------------------- #
# These tests exercise the MNQ bot with a live JarvisAdmin so every
# risk-adding action flows through the admin gate. Pattern mirrors the
# BTC hybrid bot's TestJarvisGating (tests/test_bots_btc_hybrid.py).

from datetime import UTC, datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from eta_engine.brain.jarvis_admin import (  # noqa: E402
    ActionType,
    JarvisAdmin,
    SubsystemId,
    Verdict,
    make_action_request,
)
from eta_engine.brain.jarvis_context import (  # noqa: E402
    EquitySnapshot,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    build_snapshot,
)
from eta_engine.brain.model_policy import ModelTier, TaskCategory  # noqa: E402
from eta_engine.obs.decision_journal import DecisionJournal, Outcome  # noqa: E402

_ET = ZoneInfo("America/New_York")


def _midday_ts() -> datetime:
    return datetime(2026, 4, 15, 12, 0, tzinfo=_ET).astimezone(UTC)


def _trade_ctx():  # type: ignore[no-untyped-def]
    return build_snapshot(
        macro=MacroSnapshot(vix_level=17.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=0.0,
            daily_drawdown_pct=0.0,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="TREND_UP", confidence=0.7),
        journal=JournalSnapshot(),
        ts=_midday_ts(),
    )


def _kill_ctx():  # type: ignore[no-untyped-def]
    return build_snapshot(
        macro=MacroSnapshot(vix_level=17.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=-3_000.0,
            daily_drawdown_pct=0.06,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="TREND_DOWN", confidence=0.7),
        journal=JournalSnapshot(kill_switch_active=True),
        ts=_midday_ts(),
    )


def _reduce_ctx():  # type: ignore[no-untyped-def]
    return build_snapshot(
        macro=MacroSnapshot(vix_level=17.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=-1_250.0,
            daily_drawdown_pct=0.025,
            open_positions=1,
            open_risk_r=1.0,
        ),
        regime=RegimeSnapshot(regime="TREND_UP", confidence=0.6),
        journal=JournalSnapshot(),
        ts=_midday_ts(),
    )


class TestJarvisGating:
    """MNQ bot + JarvisAdmin: every order gates through JARVIS."""

    def test_bot_subsystem_id_is_mnq(self) -> None:
        bot = MnqBot()
        assert bot.SUBSYSTEM == SubsystemId.BOT_MNQ

    @pytest.mark.asyncio
    async def test_start_asks_for_strategy_deploy_under_trade(self) -> None:
        jarvis = JarvisAdmin()
        bot = MnqBot(jarvis=jarvis, provide_ctx=_trade_ctx)
        await bot.start()
        assert bot.state.is_paused is False

    @pytest.mark.asyncio
    async def test_start_refused_under_kill(self) -> None:
        """Kill-switch context must refuse STRATEGY_DEPLOY."""
        jarvis = JarvisAdmin()
        bot = MnqBot(jarvis=jarvis, provide_ctx=_kill_ctx)
        await bot.start()
        assert bot.state.is_paused is True

    @pytest.mark.asyncio
    async def test_order_place_denied_under_kill(self) -> None:
        jarvis = JarvisAdmin()
        router = _FakeRouter([])
        bot = MnqBot(jarvis=jarvis, provide_ctx=_kill_ctx, router=router)
        sig = Signal(
            type=SignalType.LONG,
            symbol="MNQ",
            price=25_000.0,
            confidence=7.0,
            meta={"stop_distance": 5.0, "setup": "orb_breakout"},
        )
        result = await bot.on_signal(sig)
        assert result is None
        # Router MUST NOT have been called -- JARVIS denied upstream.
        assert router.calls == []

    @pytest.mark.asyncio
    async def test_conditional_size_cap_shrinks_qty(self) -> None:
        """REDUCE tier caps size at <=0.5 -- the bot must apply it."""
        jarvis = JarvisAdmin()
        router = _FakeRouter(
            [
                OrderResult(order_id="R-1", status=OrderStatus.FILLED, filled_qty=2.0, avg_price=25_000.0),
            ]
        )
        bot = MnqBot(jarvis=jarvis, provide_ctx=_reduce_ctx, router=router)
        sig = Signal(
            type=SignalType.LONG,
            symbol="MNQ",
            price=25_000.0,
            confidence=7.0,
            meta={"stop_distance": 5.0, "setup": "orb_breakout"},
        )
        result = await bot.on_signal(sig)
        # base qty would be $50 / ($10/c) = 5; cap=0.5 -> 2.
        assert result is not None
        assert router.calls[0].qty <= 3.0  # 5 * 0.5 -> int(2.5) = 2, allow drift
        assert router.calls[0].qty >= 1.0

        # Sanity: the admin itself returned CONDITIONAL for this ctx.
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
            side="LONG",
            symbol="MNQ",
            price=25_000.0,
            confidence=7.0,
        )
        resp = jarvis.request_approval(req, ctx=_reduce_ctx())
        assert resp.verdict == Verdict.CONDITIONAL
        assert resp.size_cap_mult is not None
        assert resp.size_cap_mult <= 0.5

    @pytest.mark.asyncio
    async def test_close_signals_bypass_jarvis_order_gate(self) -> None:
        """CLOSE_LONG / CLOSE_SHORT must always proceed even when JARVIS
        would refuse an entry. Exits reduce risk -- the gate is for
        entries only."""
        jarvis = JarvisAdmin()
        router = _FakeRouter(
            [
                OrderResult(order_id="C-1", status=OrderStatus.FILLED, filled_qty=2.0, avg_price=25_000.0),
            ]
        )
        bot = MnqBot(jarvis=jarvis, provide_ctx=_kill_ctx, router=router)
        sig = Signal(
            type=SignalType.CLOSE_LONG,
            symbol="MNQ",
            price=25_100.0,
            size=2.0,
            meta={},
        )
        result = await bot.on_signal(sig)
        assert result is not None
        assert router.calls[0].reduce_only is True
        assert router.calls[0].side is Side.SELL

    @pytest.mark.asyncio
    async def test_journal_records_start_and_order_events(
        self,
        tmp_path,  # type: ignore[no-untyped-def]
    ) -> None:
        journal = DecisionJournal(tmp_path / "mnq.jsonl")
        jarvis = JarvisAdmin()
        router = _FakeRouter(
            [
                OrderResult(order_id="J-1", status=OrderStatus.FILLED, filled_qty=5.0, avg_price=25_000.0),
            ]
        )
        bot = MnqBot(
            jarvis=jarvis,
            provide_ctx=_trade_ctx,
            router=router,
            journal=journal,
        )
        await bot.start()
        await bot.on_signal(
            Signal(
                type=SignalType.LONG,
                symbol="MNQ",
                price=25_000.0,
                confidence=7.0,
                meta={"stop_distance": 5.0, "setup": "orb_breakout"},
            )
        )
        await bot.stop()
        events = journal.read_all()
        intents = [e.intent for e in events]
        assert "mnq_start" in intents
        assert "mnq_order_routed" in intents
        assert "mnq_stop" in intents
        executed = [e for e in events if e.outcome == Outcome.EXECUTED]
        assert len(executed) >= 2  # start + order_routed

    @pytest.mark.asyncio
    async def test_journal_records_blocked_order(
        self,
        tmp_path,  # type: ignore[no-untyped-def]
    ) -> None:
        journal = DecisionJournal(tmp_path / "mnq_blocked.jsonl")
        jarvis = JarvisAdmin()
        bot = MnqBot(
            jarvis=jarvis,
            provide_ctx=_kill_ctx,
            router=_FakeRouter([]),
            journal=journal,
        )
        await bot.on_signal(
            Signal(
                type=SignalType.LONG,
                symbol="MNQ",
                price=25_000.0,
                confidence=7.0,
                meta={"stop_distance": 5.0, "setup": "orb_breakout"},
            )
        )
        events = journal.read_all()
        blocked = [e for e in events if e.outcome == Outcome.BLOCKED]
        assert len(blocked) == 1
        assert blocked[0].intent == "mnq_order_blocked"

    def test_pick_model_tier_without_jarvis_returns_sonnet(self) -> None:
        bot = MnqBot()
        assert bot.pick_model_tier(TaskCategory.REFACTOR) == ModelTier.SONNET

    def test_pick_model_tier_with_jarvis_routes_per_policy(self) -> None:
        jarvis = JarvisAdmin()
        bot = MnqBot(jarvis=jarvis)
        assert bot.pick_model_tier(TaskCategory.RED_TEAM_SCORING) == ModelTier.OPUS
        assert bot.pick_model_tier(TaskCategory.REFACTOR) == ModelTier.SONNET
        assert bot.pick_model_tier(TaskCategory.COMMIT_MESSAGE) == ModelTier.HAIKU

    @pytest.mark.asyncio
    async def test_no_jarvis_preserves_legacy_behavior(self) -> None:
        """When jarvis is None, on_signal routes without gating (legacy)."""
        router = _FakeRouter(
            [
                OrderResult(order_id="L-1", status=OrderStatus.FILLED, filled_qty=5.0, avg_price=25_000.0),
            ]
        )
        bot = MnqBot(router=router)  # no jarvis
        sig = Signal(
            type=SignalType.LONG,
            symbol="MNQ",
            price=25_000.0,
            confidence=7.0,
            meta={"stop_distance": 5.0, "setup": "orb_breakout"},
        )
        result = await bot.on_signal(sig)
        assert result is not None
        assert len(router.calls) == 1


class TestNqJarvisIntegration:
    """NQ bot inherits JARVIS gating from MnqBot -- verify parity."""

    def test_nq_subsystem_id(self) -> None:
        from eta_engine.bots.nq.bot import NqBot

        assert NqBot().SUBSYSTEM == SubsystemId.BOT_NQ

    @pytest.mark.asyncio
    async def test_nq_start_gates_through_jarvis(self) -> None:
        from eta_engine.bots.nq.bot import NqBot

        jarvis = JarvisAdmin()
        bot = NqBot(jarvis=jarvis, provide_ctx=_kill_ctx)
        await bot.start()
        assert bot.state.is_paused is True

    @pytest.mark.asyncio
    async def test_nq_start_approved_under_trade(self) -> None:
        from eta_engine.bots.nq.bot import NqBot

        jarvis = JarvisAdmin()
        bot = NqBot(jarvis=jarvis, provide_ctx=_trade_ctx)
        await bot.start()
        assert bot.state.is_paused is False
        await bot.stop()


class TestKillSwitchLatchGating:
    """Persistent kill-switch latch must block boot until the operator clears it."""

    @pytest.mark.asyncio
    async def test_armed_latch_allows_start(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from eta_engine.core.kill_switch_latch import KillSwitchLatch

        latch = KillSwitchLatch(tmp_path / "latch.json")
        # ARMED by default -- no file means no trip.
        bot = MnqBot(kill_switch_latch=latch)
        await bot.start()
        assert bot.state.is_paused is False
        await bot.stop()

    @pytest.mark.asyncio
    async def test_tripped_latch_blocks_start(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from eta_engine.core.kill_switch_latch import KillSwitchLatch
        from eta_engine.core.kill_switch_runtime import (
            KillAction,
            KillSeverity,
            KillVerdict,
        )

        latch = KillSwitchLatch(tmp_path / "latch.json")
        flipped = latch.record_verdict(
            KillVerdict(
                action=KillAction.FLATTEN_ALL,
                severity=KillSeverity.CRITICAL,
                reason="dd_breach_simulated",
                scope="account",
                evidence={"source": "test"},
            )
        )
        assert flipped is True
        assert latch.read().state.value == "TRIPPED"

        journal = DecisionJournal(tmp_path / "blocked.jsonl")
        bot = MnqBot(kill_switch_latch=latch, journal=journal)
        await bot.start()
        assert bot.state.is_paused is True
        # Journal should record the block.
        events = journal.read_all()
        blocked = [e for e in events if e.outcome == Outcome.BLOCKED]
        assert len(blocked) >= 1
        assert "kill_switch_latch" in blocked[0].rationale.lower()


# --------------------------------------------------------------------------- #
# D1/D4 -- SessionGate + EoD flatten integration (MnqBot.on_bar wiring)
#
# These tests exercise the bot-level wiring that ties SessionGate +
# RouterAdapter together through ``_maybe_flatten_eod``. We use a real
# RouterAdapter and a real SessionGate so the assertions hit the actual
# contract, not a mock one.
# --------------------------------------------------------------------------- #
def _ct_ms_for_bot(y: int, m: int, d: int, hh: int, mm: int) -> int:
    """(hh:mm) America/Chicago (CDT, May) -> epoch milliseconds UTC."""
    from datetime import UTC, datetime, timedelta

    utc = datetime(y, m, d, hh, mm, tzinfo=UTC) + timedelta(hours=5)
    return int(utc.timestamp() * 1000)


def _bar_for_bot(ts_ms: int, close: float = 25_000.0) -> dict:
    return {
        "ts": ts_ms,
        "open": close - 1.0,
        "high": close + 1.0,
        "low": close - 2.0,
        "close": close,
        "volume": 1000.0,
        "avg_volume": 1000.0,
        "orb_high": 0.0,
        "orb_low": 0.0,
        "ema_21": close,
        "adx_14": 22.0,
        "atr_14": 10.0,
        "vwap": close,
    }


class TestSessionGateAttachAtStart:
    """start() wires the SessionGate onto the strategy adapter."""

    @pytest.mark.asyncio
    async def test_session_gate_attached_to_adapter_on_start(self) -> None:
        from eta_engine.core.session_gate import SessionGate
        from eta_engine.strategies.engine_adapter import RouterAdapter

        adapter = RouterAdapter(asset="MNQ", session_allows_entries=True)
        assert adapter.session_gate is None
        gate = SessionGate()
        bot = MnqBot(strategy_adapter=adapter, session_gate=gate)
        await bot.start()
        assert adapter.session_gate is gate
        await bot.stop()

    @pytest.mark.asyncio
    async def test_no_session_gate_leaves_adapter_unchanged(self) -> None:
        from eta_engine.strategies.engine_adapter import RouterAdapter

        adapter = RouterAdapter(asset="MNQ", session_allows_entries=True)
        bot = MnqBot(strategy_adapter=adapter, session_gate=None)
        await bot.start()
        assert adapter.session_gate is None
        await bot.stop()

    @pytest.mark.asyncio
    async def test_no_adapter_with_gate_is_safe(self) -> None:
        """Bot without an adapter should not crash when session_gate is set."""
        from eta_engine.core.session_gate import SessionGate

        bot = MnqBot(strategy_adapter=None, session_gate=SessionGate())
        await bot.start()
        assert bot.state.is_paused is False
        await bot.stop()


class TestEodFlattenSignalEmission:
    """_maybe_flatten_eod emits close signals through on_signal."""

    def _wire_bot(self) -> tuple[MnqBot, list[Signal]]:
        """Build a MnqBot + narrow-RTH gate + capturing on_signal stub."""
        from datetime import time

        from eta_engine.core.session_gate import (
            SessionGate,
            SessionGateConfig,
        )
        from eta_engine.strategies.engine_adapter import RouterAdapter

        cfg = SessionGateConfig(
            rth_start_local=time(8, 30),
            rth_end_local=time(16, 0),
            eod_cutoff_local=time(15, 59),
        )
        gate = SessionGate(config=cfg)
        adapter = RouterAdapter(asset="MNQ", session_allows_entries=True)
        bot = MnqBot(strategy_adapter=adapter, session_gate=gate)

        captured: list[Signal] = []

        async def _capture(sig: Signal) -> None:
            captured.append(sig)

        bot.on_signal = _capture  # type: ignore[method-assign]
        return bot, captured

    @pytest.mark.asyncio
    async def test_eod_flatten_fires_close_signals(self) -> None:
        from eta_engine.core.session_gate import REASON_EOD_PENDING

        bot, captured = self._wire_bot()
        await bot.start()
        # Seed one long + one short position so the bot emits one
        # CLOSE_LONG and one CLOSE_SHORT.
        bot.state.open_positions = [
            Position(symbol="MNQ", side="long", entry_price=25_000.0, size=1),
            Position(symbol="MNQ", side="short", entry_price=25_010.0, size=1),
        ]
        # Bar at 15:59 CT -> past EoD cutoff.
        bar = _bar_for_bot(_ct_ms_for_bot(2026, 5, 15, 15, 59))
        await bot.on_bar(bar)

        assert len(captured) == 2
        types = {s.type for s in captured}
        assert SignalType.CLOSE_LONG in types
        assert SignalType.CLOSE_SHORT in types
        for sig in captured:
            assert sig.meta["setup"] == "eod_flatten"
            assert sig.meta["source"] == "session_gate"
            assert sig.meta["gate_reason"] == REASON_EOD_PENDING
            assert sig.price == 25_000.0  # last close from bar
        await bot.stop()

    @pytest.mark.asyncio
    async def test_eod_flatten_no_positions_is_noop(self) -> None:
        """Fires the journal event but emits zero signals when flat."""
        bot, captured = self._wire_bot()
        await bot.start()
        bot.state.open_positions = []
        bar = _bar_for_bot(_ct_ms_for_bot(2026, 5, 15, 15, 59))
        await bot.on_bar(bar)
        # The latch still tripped but no signals to emit.
        assert captured == []
        assert bot._eod_flatten_fired is True
        await bot.stop()

    @pytest.mark.asyncio
    async def test_eod_flatten_only_fires_once_per_window(self) -> None:
        """Two bars past the cutoff -> one emission, not two."""
        bot, captured = self._wire_bot()
        await bot.start()
        bot.state.open_positions = [
            Position(symbol="MNQ", side="long", entry_price=25_000.0, size=1),
        ]
        # Bar A: 15:59 CT -> fires.
        bar_a = _bar_for_bot(_ct_ms_for_bot(2026, 5, 15, 15, 59))
        await bot.on_bar(bar_a)
        assert len(captured) == 1
        # Bar B: 15:59 CT with an updated close; past cutoff still.
        # RouterAdapter would repopulate state.open_positions in a real
        # fill loop; for this test the point is that _maybe_flatten_eod
        # is latched and does NOT re-emit even if positions reappear.
        bot.state.open_positions = [
            Position(symbol="MNQ", side="long", entry_price=25_000.0, size=1),
        ]
        bar_b = _bar_for_bot(_ct_ms_for_bot(2026, 5, 15, 15, 59), close=25_005.0)
        await bot.on_bar(bar_b)
        assert len(captured) == 1
        await bot.stop()

    @pytest.mark.asyncio
    async def test_eod_flatten_latch_resets_on_new_rth_session(self) -> None:
        """After gate returns to no_eod_action, the latch resets so the next
        cutoff can fire."""
        bot, captured = self._wire_bot()
        await bot.start()
        bot.state.open_positions = [
            Position(symbol="MNQ", side="long", entry_price=25_000.0, size=1),
        ]
        # Fire once at cutoff.
        await bot.on_bar(
            _bar_for_bot(_ct_ms_for_bot(2026, 5, 15, 15, 59)),
        )
        assert len(captured) == 1
        assert bot._eod_flatten_fired is True
        # Mid-RTH next day: gate reports no_eod_action -> latch resets.
        await bot.on_bar(
            _bar_for_bot(_ct_ms_for_bot(2026, 5, 16, 10, 0)),
        )
        assert bot._eod_flatten_fired is False
        # Back past cutoff -> fires again.
        await bot.on_bar(
            _bar_for_bot(_ct_ms_for_bot(2026, 5, 16, 15, 59)),
        )
        assert len(captured) == 2
        await bot.stop()

    @pytest.mark.asyncio
    async def test_eod_flatten_no_adapter_is_noop(self) -> None:
        """Without an adapter the bot still boots; on_bar doesn't crash."""
        from eta_engine.core.session_gate import SessionGate

        bot = MnqBot(strategy_adapter=None, session_gate=SessionGate())
        captured: list[Signal] = []

        async def _capture(sig: Signal) -> None:
            captured.append(sig)

        bot.on_signal = _capture  # type: ignore[method-assign]
        await bot.start()
        bot.state.open_positions = [
            Position(symbol="MNQ", side="long", entry_price=25_000.0, size=1),
        ]
        # 15:59 CT would have fired if an adapter were wired; w/o adapter
        # the _maybe_flatten_eod short-circuits before reaching the gate.
        await bot.on_bar(
            _bar_for_bot(_ct_ms_for_bot(2026, 5, 15, 15, 59)),
        )
        assert captured == []
        await bot.stop()
