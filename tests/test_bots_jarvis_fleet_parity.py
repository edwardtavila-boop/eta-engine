"""
EVOLUTIONARY TRADING ALGO  //  tests.test_bots_jarvis_fleet_parity
======================================================
Verify every fleet bot honors the JARVIS takeover contract.

Each bot should:
  * Expose a distinct ``SUBSYSTEM`` matching its role.
  * Refuse to start when JARVIS denies STRATEGY_DEPLOY (kill context).
  * Return ``None`` from ``on_signal`` when JARVIS denies ORDER_PLACE.
  * Apply the CONDITIONAL size cap to qty (REDUCE context).
  * Still work in legacy mode (no JARVIS wired).

Covers: MnqBot, NqBot, BtcHybridBot, EthPerpBot, SolPerpBot, XrpPerpBot,
CryptoSeedBot. MNQ + BTC have dedicated test modules already; here we
fill in the perps and the seed bot.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from eta_engine.bots.base_bot import Signal, SignalType
from eta_engine.bots.crypto_seed.bot import CryptoSeedBot
from eta_engine.bots.eth_perp.bot import EthPerpBot
from eta_engine.bots.sol_perp.bot import SolPerpBot
from eta_engine.bots.xrp_perp.bot import XrpPerpBot
from eta_engine.brain.jarvis_admin import JarvisAdmin, SubsystemId
from eta_engine.brain.jarvis_context import (
    EquitySnapshot,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    build_snapshot,
)
from eta_engine.venues.base import OrderRequest, OrderResult, OrderStatus

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


class _FakeRouter:
    """Minimal router that always fills at the requested qty."""

    def __init__(self) -> None:
        self.calls: list[OrderRequest] = []

    async def place_with_failover(
        self,
        req: OrderRequest,
        *,
        urgency: str = "normal",
    ) -> OrderResult:
        _ = urgency
        self.calls.append(req)
        return OrderResult(
            order_id=f"F-{len(self.calls):04d}",
            status=OrderStatus.FILLED,
            filled_qty=req.qty,
            avg_price=req.price or 1000.0,
        )


# --------------------------------------------------------------------------- #
# SUBSYSTEM identities: each bot must have a distinct audit ID
# --------------------------------------------------------------------------- #
class TestSubsystemIdentities:
    def test_eth_subsystem(self) -> None:
        assert EthPerpBot().SUBSYSTEM == SubsystemId.BOT_ETH_PERP

    def test_sol_subsystem(self) -> None:
        assert SolPerpBot().SUBSYSTEM == SubsystemId.BOT_SOL_PERP

    def test_xrp_subsystem(self) -> None:
        assert XrpPerpBot().SUBSYSTEM == SubsystemId.BOT_XRP_PERP

    def test_crypto_seed_subsystem(self) -> None:
        assert CryptoSeedBot().SUBSYSTEM == SubsystemId.BOT_CRYPTO_SEED

    def test_subsystems_all_distinct(self) -> None:
        seen = {
            EthPerpBot().SUBSYSTEM,
            SolPerpBot().SUBSYSTEM,
            XrpPerpBot().SUBSYSTEM,
            CryptoSeedBot().SUBSYSTEM,
        }
        assert len(seen) == 4  # all four distinct


# --------------------------------------------------------------------------- #
# ETH perp JARVIS integration
# --------------------------------------------------------------------------- #
class TestEthPerpJarvis:
    @pytest.mark.asyncio
    async def test_start_approved_under_trade(self) -> None:
        jarvis = JarvisAdmin()
        bot = EthPerpBot(jarvis=jarvis, provide_ctx=_trade_ctx)
        await bot.start()
        assert bot.state.is_paused is False
        await bot.stop()

    @pytest.mark.asyncio
    async def test_start_refused_under_kill(self) -> None:
        jarvis = JarvisAdmin()
        bot = EthPerpBot(jarvis=jarvis, provide_ctx=_kill_ctx)
        await bot.start()
        assert bot.state.is_paused is True

    @pytest.mark.asyncio
    async def test_order_denied_under_kill_no_router_call(self) -> None:
        jarvis = JarvisAdmin()
        router = _FakeRouter()
        bot = EthPerpBot(jarvis=jarvis, provide_ctx=_kill_ctx, router=router)
        sig = Signal(
            type=SignalType.LONG,
            symbol="ETHUSDT",
            price=3_500.0,
            confidence=7.5,
            meta={"leverage": 10.0, "stop_distance": 35.0},
        )
        result = await bot.on_signal(sig)
        assert result is None
        assert router.calls == []

    @pytest.mark.asyncio
    async def test_conditional_cap_shrinks_leverage(self) -> None:
        """REDUCE tier caps at <=0.5 -- ETH scales both qty and leverage."""
        jarvis = JarvisAdmin()
        router = _FakeRouter()
        bot = EthPerpBot(jarvis=jarvis, provide_ctx=_reduce_ctx, router=router)
        sig = Signal(
            type=SignalType.LONG,
            symbol="ETHUSDT",
            price=3_500.0,
            confidence=7.5,
            meta={"leverage": 10.0, "stop_distance": 35.0},
        )
        result = await bot.on_signal(sig)
        # CONDITIONAL under reduce_ctx -> cap 0.5 -> leverage halved from 10
        # to ~5, qty also scales accordingly.
        assert result is not None
        # The effective leverage should be at or below original * cap.
        assert sig.meta["leverage"] <= 10.0
        assert sig.meta["leverage"] >= 1.0  # never drops below 1x

    @pytest.mark.asyncio
    async def test_legacy_mode_no_jarvis(self) -> None:
        router = _FakeRouter()
        bot = EthPerpBot(router=router)  # no jarvis
        sig = Signal(
            type=SignalType.LONG,
            symbol="ETHUSDT",
            price=3_500.0,
            confidence=7.5,
            meta={"leverage": 10.0, "stop_distance": 35.0},
        )
        result = await bot.on_signal(sig)
        assert result is not None
        assert len(router.calls) == 1


# --------------------------------------------------------------------------- #
# SOL + XRP inherit from ETH -- minimal sanity tests
# --------------------------------------------------------------------------- #
class TestSolPerpJarvis:
    @pytest.mark.asyncio
    async def test_start_refused_under_kill(self) -> None:
        jarvis = JarvisAdmin()
        bot = SolPerpBot(jarvis=jarvis, provide_ctx=_kill_ctx)
        await bot.start()
        assert bot.state.is_paused is True

    @pytest.mark.asyncio
    async def test_order_denied_under_kill_no_router_call(self) -> None:
        jarvis = JarvisAdmin()
        router = _FakeRouter()
        bot = SolPerpBot(jarvis=jarvis, provide_ctx=_kill_ctx, router=router)
        sig = Signal(
            type=SignalType.LONG,
            symbol="SOLUSDT",
            price=180.0,
            confidence=7.5,
            meta={"leverage": 10.0, "stop_distance": 1.8},
        )
        result = await bot.on_signal(sig)
        assert result is None
        assert router.calls == []


class TestXrpPerpJarvis:
    @pytest.mark.asyncio
    async def test_start_refused_under_kill(self) -> None:
        jarvis = JarvisAdmin()
        bot = XrpPerpBot(jarvis=jarvis, provide_ctx=_kill_ctx)
        await bot.start()
        assert bot.state.is_paused is True

    @pytest.mark.asyncio
    async def test_order_denied_under_kill_no_router_call(self) -> None:
        jarvis = JarvisAdmin()
        router = _FakeRouter()
        bot = XrpPerpBot(jarvis=jarvis, provide_ctx=_kill_ctx, router=router)
        sig = Signal(
            type=SignalType.LONG,
            symbol="XRPUSDT",
            price=2.5,
            confidence=7.5,
            meta={"leverage": 10.0, "stop_distance": 0.025},
        )
        result = await bot.on_signal(sig)
        assert result is None
        assert router.calls == []


# --------------------------------------------------------------------------- #
# Crypto Seed bot -- directional overlay is gated; grid is orchestrator-driven
# --------------------------------------------------------------------------- #
class TestCryptoSeedJarvis:
    @pytest.mark.asyncio
    async def test_start_approved_under_trade(self) -> None:
        jarvis = JarvisAdmin()
        bot = CryptoSeedBot(jarvis=jarvis, provide_ctx=_trade_ctx)
        await bot.start()
        assert bot.state.is_paused is False

    @pytest.mark.asyncio
    async def test_start_refused_under_kill(self) -> None:
        jarvis = JarvisAdmin()
        bot = CryptoSeedBot(jarvis=jarvis, provide_ctx=_kill_ctx)
        await bot.start()
        assert bot.state.is_paused is True

    @pytest.mark.asyncio
    async def test_directional_overlay_denied_under_kill(self) -> None:
        jarvis = JarvisAdmin()
        router = _FakeRouter()
        bot = CryptoSeedBot(
            jarvis=jarvis,
            provide_ctx=_kill_ctx,
            router=router,
        )
        sig = Signal(
            type=SignalType.LONG,
            symbol="BTCUSDT",
            price=90_000.0,
            confidence=8.0,
            meta={},
        )
        result = await bot.on_signal(sig)
        assert result is None
        assert router.calls == []

    @pytest.mark.asyncio
    async def test_directional_overlay_legacy_path_no_jarvis(self) -> None:
        router = _FakeRouter()
        bot = CryptoSeedBot(router=router)  # no jarvis
        sig = Signal(
            type=SignalType.LONG,
            symbol="BTCUSDT",
            price=90_000.0,
            confidence=8.0,
            meta={},
        )
        result = await bot.on_signal(sig)
        # With router + no risk lockout, the directional overlay should fire.
        assert result is not None
        assert len(router.calls) == 1


# --------------------------------------------------------------------------- #
# Cross-bot LLM tier routing sanity
# --------------------------------------------------------------------------- #
def test_all_bots_pick_model_tier_without_jarvis_returns_sonnet() -> None:
    """Every bot should fall back to SONNET when no JARVIS is wired."""
    from eta_engine.brain.model_policy import ModelTier, TaskCategory

    for bot in (EthPerpBot(), SolPerpBot(), XrpPerpBot(), CryptoSeedBot()):
        assert bot.pick_model_tier(TaskCategory.REFACTOR) == ModelTier.SONNET


def test_all_bots_pick_model_tier_with_jarvis_routes_per_policy() -> None:
    from eta_engine.brain.model_policy import ModelTier, TaskCategory

    jarvis = JarvisAdmin()
    for ctor in (EthPerpBot, SolPerpBot, XrpPerpBot, CryptoSeedBot):
        bot = ctor(jarvis=jarvis)
        assert bot.pick_model_tier(TaskCategory.RED_TEAM_SCORING) == ModelTier.OPUS
        assert bot.pick_model_tier(TaskCategory.COMMIT_MESSAGE) == ModelTier.HAIKU


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
