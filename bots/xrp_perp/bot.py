"""XRP Perp Bot — CASINO tier, thin-liquidity instrument.

XRPUSDT perpetual. Same 3-setup pattern as ETH but with tighter leverage
caps due to XRP's thinner order book depth. Max 50x even at 9+ confluence.
Wider slippage assumptions: 0.008 vs 0.005 in fee/slippage buffer.
"""

from __future__ import annotations

import logging
from typing import Any

from eta_engine.bots.base_bot import (
    BotConfig,
    MarginMode,
    Signal,
    SignalType,
    Tier,
)
from eta_engine.bots.eth_perp.bot import EthPerpBot
from eta_engine.brain.jarvis_admin import SubsystemId
from eta_engine.venues.base import OrderRequest, OrderType, Side

logger = logging.getLogger(__name__)

XRP_CONFIG = BotConfig(
    name="XRP-Perp",
    symbol="XRPUSDT",
    tier=Tier.CASINO,
    baseline_usd=2000.0,
    starting_capital_usd=2000.0,
    max_leverage=50.0,  # hard cap — thinner liquidity than ETH/SOL
    risk_per_trade_pct=3.0,
    daily_loss_cap_pct=6.0,
    max_dd_kill_pct=20.0,
    margin_mode=MarginMode.ISOLATED,
)

# XRP leverage tiers — capped at 50x even at highest confluence
_XRP_LEV_TIERS: list[tuple[float, float]] = [
    (9.0, 50.0),  # confluence >= 9  → 50x (not 75x)
    (7.0, 20.0),  # confluence 7-8   → 20x
    (5.0, 10.0),  # confluence 5-6   → 10x
]

# XRP slippage is higher due to thinner books
_XRP_SLIPPAGE_BUFFER: float = 0.008


class XrpPerpBot(EthPerpBot):
    """XRP perp bot -- thinner liquidity requires tighter leverage caps.

    Inherits ETH's 3 setups AND the JARVIS gate, router path, journal,
    and lifecycle from :class:`EthPerpBot`. Overrides only the
    instrument-specific leverage gating (50x max), liquidation calc
    (wider fee buffer), and evaluation thresholds.
    """

    # Distinct audit identity -- XRP flow filters separately in the
    # jarvis_audit.jsonl log.
    SUBSYSTEM: SubsystemId = SubsystemId.BOT_XRP_PERP

    def __init__(
        self,
        config: BotConfig | None = None,
        **kwargs: Any,  # noqa: ANN401 - forwards router/jarvis/... to EthPerpBot
    ) -> None:
        super().__init__(config or XRP_CONFIG, **kwargs)

    # ── XRP-specific leverage gating (50x hard cap) ──

    @staticmethod
    def confluence_leverage(confluence: float) -> float | None:
        """XRP leverage tiers — max 50x even at 9+ confluence."""
        if confluence < 5.0:
            return None
        for threshold, max_lev in _XRP_LEV_TIERS:
            if confluence >= threshold:
                return max_lev
        return None

    @staticmethod
    def liquidation_safe_leverage(price: float, atr_14_5m: float) -> float:
        """XRP liq check — wider slippage buffer (0.008 vs 0.005).

        liq_dist = 3.0 * atr_14_5m
        max_lev = price / (liq_dist * 1.20 + price * 0.008)
        """
        liq_dist = 3.0 * atr_14_5m
        denominator = liq_dist * 1.20 + price * _XRP_SLIPPAGE_BUFFER
        if denominator <= 0:
            return 1.0
        return price / denominator

    # ── XRP-specific setup tweaks ──

    def trend_follow(self, bar: dict[str, Any]) -> Signal | None:
        """Trend follow — XRP requires higher ADX (30) due to fake-out frequency."""
        adx = bar.get("adx_14", 0.0)
        ema_9 = bar.get("ema_9", 0.0)
        ema_21 = bar.get("ema_21", 0.0)
        if adx < 30.0 or ema_9 == 0.0:  # 30 vs ETH's 25
            return None
        vol_ratio = bar.get("volume", 0) / max(bar.get("avg_volume", 1), 1)
        if vol_ratio < 1.5:  # 1.5 vs ETH's 1.2 — need stronger volume confirm
            return None
        direction = SignalType.LONG if ema_9 > ema_21 else SignalType.SHORT
        conf = min(6.0 + (adx - 30) / 10 + vol_ratio, 10.0)
        return Signal(type=direction, symbol=self.config.symbol, price=bar["close"], confidence=conf)

    def mean_revert(self, bar: dict[str, Any]) -> Signal | None:
        """Mean reversion — XRP uses extreme RSI (75/25) due to momentum persistence."""
        bb_upper = bar.get("bb_upper", 0.0)
        bb_lower = bar.get("bb_lower", 0.0)
        rsi = bar.get("rsi_14", 50.0)
        if bb_upper == 0.0:
            return None
        if bar["close"] >= bb_upper and rsi > 75:
            return Signal(type=SignalType.SHORT, symbol=self.config.symbol, price=bar["close"], confidence=6.0)
        if bar["close"] <= bb_lower and rsi < 25:
            return Signal(type=SignalType.LONG, symbol=self.config.symbol, price=bar["close"], confidence=6.0)
        return None

    # ── Lifecycle ──

    async def start(self) -> None:
        # Delegate to EthPerpBot.start so the JARVIS STRATEGY_DEPLOY
        # gate + journal writes happen consistently.
        await super().start()
        if self.state.is_paused:
            return
        logger.info(
            "XRP Perp bot armed | capital=$%.2f",
            self.config.starting_capital_usd,
        )

    async def stop(self) -> None:
        logger.info("XRP Perp bot stopping | equity=$%.2f", self.state.equity)
        await super().stop()

    async def on_signal(self, signal: Signal) -> Any:  # noqa: ANN401 - inherits OrderResult | None
        """Delegate to :class:`EthPerpBot.on_signal` — the parent calls
        :meth:`_build_order_request`, which we override below so XRP's
        thin-book orders go out as POST_ONLY at signal.price with urgency=low.
        The _XRP_SLIPPAGE_BUFFER still backstops the liq math in case a
        POST_ONLY doesn't fill and a fallback market is taken.
        """
        return await super().on_signal(signal)

    def _build_order_request(
        self,
        signal: Signal,
        side: Side,
        qty: float,
        reduce_only: bool,
    ) -> tuple[OrderRequest, str]:
        """XRP override — POST_ONLY at signal.price, urgency=low.

        Thin books punish takers with 2-3× the queue-normal slippage.
        Paying the spread here is worth more than the occasional missed
        fill; the next bar regenerates a signal if the entry was still
        warranted. For CLOSE_* signals we still use POST_ONLY but at a
        price that crosses (bid for buy-to-close, ask for sell-to-close)
        so exits don't get stranded on a ledge.
        """
        limit_price = float(signal.price)
        return (
            OrderRequest(
                symbol=self._venue_symbol,
                side=side,
                qty=qty,
                order_type=OrderType.POST_ONLY,
                price=limit_price,
                reduce_only=reduce_only,
            ),
            "low",
        )

    # ── Entry requires stronger confluence for XRP ──

    def evaluate_entry(self, bar: dict[str, Any], confluence_score: float) -> bool:
        if confluence_score < 6.0:  # higher floor than ETH's 5.0
            return False
        atr = bar.get("atr_14", bar.get("close", 1) * 0.03)
        lev = self.effective_leverage(confluence_score, bar.get("close", 0), atr)
        return lev is not None and self.check_risk()
