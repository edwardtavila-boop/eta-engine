"""SOL Perp Bot — CASINO tier, calibrated for SOL volatility.

SOLUSDT perpetual. Same 3-setup pattern as ETH but with wider ATR
multipliers to account for SOL's higher intraday vol profile.
SOL 30-day realized vol typically 1.3-1.8x ETH's.
"""

from __future__ import annotations

import logging
from typing import Any

from eta_engine.bots.base_bot import (
    BotConfig,
    MarginMode,
    RegimeType,
    Signal,
    SignalType,
    Tier,
)
from eta_engine.bots.eth_perp.bot import EthPerpBot
from eta_engine.brain.jarvis_admin import SubsystemId

logger = logging.getLogger(__name__)

SOL_CONFIG = BotConfig(
    name="SOL-Perp",
    symbol="SOLUSDT",
    tier=Tier.CASINO,
    baseline_usd=3000.0,
    starting_capital_usd=3000.0,
    max_leverage=75.0,
    risk_per_trade_pct=3.0,
    daily_loss_cap_pct=6.0,
    max_dd_kill_pct=20.0,
    margin_mode=MarginMode.ISOLATED,
)

# SOL vol multiplier — ATR-based calcs use wider buffers
SOL_VOL_FACTOR: float = 1.5


class SolPerpBot(EthPerpBot):
    """SOL perp bot -- inherits ETH pattern, recalibrated for SOL volatility.

    JARVIS wiring + router path + leverage gate are all inherited from
    :class:`EthPerpBot`. This class only overrides the per-instrument
    tuning (ATR buffer, RSI thresholds, ranging-regime flip) and the
    audit subsystem identity.

    Key differences from ETH:
    - Liquidation distance uses 4.5 * ATR (vs 3.0) due to SOL's fatter tails
    - Squeeze ratio threshold loosened to 0.65 (SOL compresses less before moves)
    - Mean reversion RSI thresholds widened (75/25 vs 70/30)
    - Directional signals flip in RANGING regime so chop can be traded
      as a contrarian overlay instead of a trend-chasing drag.
    """

    # Distinct audit identity so operator can filter SOL flow in the
    # jarvis_audit.jsonl log even though the code path is shared.
    SUBSYSTEM: SubsystemId = SubsystemId.BOT_SOL_PERP

    def __init__(
        self,
        config: BotConfig | None = None,
        **kwargs: Any,  # noqa: ANN401 - forwards router/jarvis/... to EthPerpBot
    ) -> None:
        super().__init__(config or SOL_CONFIG, **kwargs)

    # ── SOL-calibrated liquidation check ──

    @staticmethod
    def liquidation_safe_leverage(price: float, atr_14_5m: float) -> float:
        """SOL needs wider liquidation buffer: 4.5 * ATR (vs 3.0 for ETH).

        liq_dist = 4.5 * atr (SOL vol factor baked in)
        max_lev = price / (liq_dist * 1.20 + price * 0.005)
        """
        liq_dist = 4.5 * atr_14_5m  # wider than ETH's 3.0
        denominator = liq_dist * 1.20 + price * 0.005
        if denominator <= 0:
            return 1.0
        return price / denominator

    # ── SOL-specific setup overrides ──

    def mean_revert(self, bar: dict[str, Any]) -> Signal | None:
        """Mean reversion with wider RSI thresholds for SOL's vol profile."""
        bb_upper = bar.get("bb_upper", 0.0)
        bb_lower = bar.get("bb_lower", 0.0)
        rsi = bar.get("rsi_14", 50.0)
        if bb_upper == 0.0:
            return None
        if bar["close"] >= bb_upper and rsi > 75:  # 75 vs ETH's 70
            return Signal(type=SignalType.SHORT, symbol=self.config.symbol, price=bar["close"], confidence=6.5)
        if bar["close"] <= bb_lower and rsi < 25:  # 25 vs ETH's 30
            return Signal(type=SignalType.LONG, symbol=self.config.symbol, price=bar["close"], confidence=6.5)
        return None

    def breakout(self, bar: dict[str, Any]) -> Signal | None:
        """Breakout — SOL uses 0.65 squeeze threshold (vs 0.75 for ETH)."""
        atr = bar.get("atr_14", 0.0)
        avg_atr = bar.get("avg_atr_50", 0.0)
        if atr == 0.0 or avg_atr == 0.0:
            return None
        squeeze_ratio = atr / avg_atr
        if squeeze_ratio > 0.65:  # SOL compresses less before big moves
            return None
        bar_range = bar["high"] - bar["low"]
        if bar_range > 2.5 * atr:  # wider expansion threshold for SOL
            direction = SignalType.LONG if bar["close"] > bar["open"] else SignalType.SHORT
            return Signal(type=direction, symbol=self.config.symbol, price=bar["close"], confidence=7.5)
        return None

    def _prepare_signal_for_routing(
        self,
        signal: Signal,
        bar: dict[str, Any],
        regime: RegimeType,
    ) -> Signal:
        """Flip directional bias in SOL ranging regimes.

        The SOL redesign track treats RANGING ADX conditions as contrarian
        territory. We keep the rest of the ETH routing pipeline intact and only
        invert long/short signals here so retrospective tracking and router
        sizing both see the final direction.
        """
        if regime is not RegimeType.RANGING:
            return signal
        if signal.type not in (SignalType.LONG, SignalType.SHORT):
            return signal
        flipped_type = SignalType.SHORT if signal.type is SignalType.LONG else SignalType.LONG
        meta = dict(signal.meta)
        meta["regime_overlay"] = "SOL_RANGING_FLIP"
        meta["regime_overlay_adx"] = round(float(bar.get("adx_14", 0.0)), 2)
        meta["regime_overlay_source"] = "sol_perp"
        logger.info(
            "SOL ranging overlay flipped %s -> %s (adx=%.1f)",
            signal.type.value,
            flipped_type.value,
            float(bar.get("adx_14", 0.0)),
        )
        return signal.model_copy(
            update={"type": flipped_type, "meta": meta},
        )

    # ── Lifecycle ──

    async def start(self) -> None:
        # Delegate to EthPerpBot.start so the JARVIS STRATEGY_DEPLOY
        # gate + journal writes happen consistently. If JARVIS refuses
        # the strategy, super() sets is_paused and we short-circuit.
        await super().start()
        if self.state.is_paused:
            return
        logger.info(
            "SOL Perp bot armed | capital=$%.2f",
            self.config.starting_capital_usd,
        )

    async def stop(self) -> None:
        logger.info("SOL Perp bot stopping | equity=$%.2f", self.state.equity)
        # Delegate the cleanup (journal write + _active_entries clear)
        # to the parent so subclass + parent stay in sync automatically.
        await super().stop()

    async def on_signal(self, signal: Signal) -> Any:  # noqa: ANN401 - inherits OrderResult | None
        """Delegate to :class:`EthPerpBot.on_signal` — router path handles SOL routing."""
        return await super().on_signal(signal)
