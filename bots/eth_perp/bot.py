"""ETH Perp Bot -- CASINO tier, leverage-gated by confluence.

Liquidation-proof: max_lev = price / (3 * ATR14_5m * 1.20 + price * 0.005).

Injectable dependencies
-----------------------
Same contract as :class:`eta_engine.bots.mnq.bot.MnqBot`:

* ``router`` — anything exposing ``async place_with_failover(OrderRequest)``.
  When supplied, ``on_signal`` routes market orders to the venue with the
  ``leverage`` recorded in ``meta``. The adapter layer is expected to send
  ``set_leverage(symbol, lev)`` before the order (see ``venues.bybit``).
* ``venue_symbol`` — exchange-specific contract symbol. Defaults to ``config.symbol``.

Subclasses (SOL, XRP) inherit the router path — they only override the
per-instrument setup thresholds and leverage math.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from eta_engine.bots.base_bot import (
    BaseBot,
    BotConfig,
    MarginMode,
    Position,
    Signal,
    SignalType,
    Tier,
)
from eta_engine.venues.base import OrderRequest, OrderResult, OrderStatus, Side

if TYPE_CHECKING:
    from eta_engine.strategies.engine_adapter import RouterAdapter

logger = logging.getLogger(__name__)
ETH_CONFIG = BotConfig(
    name="ETH-Perp", symbol="ETHUSDT", tier=Tier.CASINO, baseline_usd=3000.0,
    starting_capital_usd=3000.0, max_leverage=75.0, risk_per_trade_pct=3.0,
    daily_loss_cap_pct=6.0, max_dd_kill_pct=20.0, margin_mode=MarginMode.ISOLATED,
)
_LEV_TIERS: list[tuple[float, float]] = [(9.0, 75.0), (7.0, 20.0), (5.0, 10.0)]
_LEV_MIN_CONFLUENCE: float = 5.0


class _Router(Protocol):
    async def place_with_failover(self, req: OrderRequest) -> OrderResult: ...


class EthPerpBot(BaseBot):
    """ETH perp bot — 3 directional setups, liquidation-proof leverage, router-backed."""

    # Exposed so subclasses (SOL, XRP) can replace the leverage grid in one line.
    LEV_TIERS: list[tuple[float, float]] = _LEV_TIERS
    LEV_MIN_CONFLUENCE: float = _LEV_MIN_CONFLUENCE

    def __init__(
        self,
        config: BotConfig | None = None,
        *,
        router: _Router | None = None,
        venue_symbol: str | None = None,
        strategy_adapter: RouterAdapter | None = None,
    ) -> None:
        super().__init__(config or ETH_CONFIG)
        self._router = router
        self._venue_symbol = venue_symbol or self.config.symbol
        self._strategy_adapter = strategy_adapter

    # ── Leverage Gating ──

    @classmethod
    def confluence_leverage(cls, confluence: float) -> float | None:
        """Return max allowed leverage for a given confluence score, or None to reject."""
        if confluence < cls.LEV_MIN_CONFLUENCE:
            return None
        for threshold, max_lev in cls.LEV_TIERS:
            if confluence >= threshold:
                return max_lev
        return None

    @staticmethod
    def liquidation_safe_leverage(price: float, atr_14_5m: float) -> float:
        """Max leverage that keeps liquidation > 3 * ATR away from entry.

        liq_dist_required = 3.0 * atr_14_5m
        max_lev = price / (liq_dist_required * 1.20 + price * 0.005)
        The 1.20 adds 20% buffer; 0.005 covers funding + fees.
        """
        liq_dist = 3.0 * atr_14_5m
        denominator = liq_dist * 1.20 + price * 0.005
        if denominator <= 0:
            return 1.0
        return price / denominator

    def effective_leverage(self, confluence: float, price: float, atr: float) -> float | None:
        """Final leverage = min(confluence_tier, liq_safe). None = reject."""
        tier_lev = self.confluence_leverage(confluence)
        if tier_lev is None:
            return None
        safe_lev = self.liquidation_safe_leverage(price, atr)
        return min(tier_lev, safe_lev, self.config.max_leverage)

    # ── Lifecycle ──

    async def start(self) -> None:
        logger.info(
            "ETH Perp bot starting | capital=$%.2f symbol=%s router=%s",
            self.config.starting_capital_usd, self._venue_symbol,
            "yes" if self._router is not None else "no",
        )

    async def stop(self) -> None:
        logger.info("ETH Perp bot stopping | equity=$%.2f", self.state.equity)

    # ── 3 Directional Setups ──

    def trend_follow(self, bar: dict[str, Any]) -> Signal | None:
        """Trend follow — EMA stack + ADX > 25 + volume spike."""
        adx = bar.get("adx_14", 0.0)
        ema_9 = bar.get("ema_9", 0.0)
        ema_21 = bar.get("ema_21", 0.0)
        if adx < 25.0 or ema_9 == 0.0:
            return None
        vol_ratio = bar.get("volume", 0) / max(bar.get("avg_volume", 1), 1)
        if vol_ratio < 1.2:
            return None
        direction = SignalType.LONG if ema_9 > ema_21 else SignalType.SHORT
        conf = min(6.0 + (adx - 25) / 10 + vol_ratio, 10.0)
        return Signal(type=direction, symbol=self.config.symbol, price=bar["close"], confidence=conf)

    def mean_revert(self, bar: dict[str, Any]) -> Signal | None:
        """Mean reversion — Bollinger band touch + RSI divergence."""
        bb_upper = bar.get("bb_upper", 0.0)
        bb_lower = bar.get("bb_lower", 0.0)
        rsi = bar.get("rsi_14", 50.0)
        if bb_upper == 0.0:
            return None
        if bar["close"] >= bb_upper and rsi > 70:
            return Signal(type=SignalType.SHORT, symbol=self.config.symbol, price=bar["close"], confidence=6.5)
        if bar["close"] <= bb_lower and rsi < 30:
            return Signal(type=SignalType.LONG, symbol=self.config.symbol, price=bar["close"], confidence=6.5)
        return None

    def breakout(self, bar: dict[str, Any]) -> Signal | None:
        """Breakout — range compression then expansion with volume confirm."""
        atr = bar.get("atr_14", 0.0)
        avg_atr = bar.get("avg_atr_50", 0.0)
        if atr == 0.0 or avg_atr == 0.0:
            return None
        squeeze_ratio = atr / avg_atr
        if squeeze_ratio > 0.75:
            return None
        bar_range = bar["high"] - bar["low"]
        if bar_range > 2.0 * atr:
            direction = SignalType.LONG if bar["close"] > bar["open"] else SignalType.SHORT
            return Signal(type=direction, symbol=self.config.symbol, price=bar["close"], confidence=7.5)
        return None

    # ── Market Events ──

    async def on_bar(self, bar: dict[str, Any]) -> None:
        if not self.check_risk():
            return
        # AI-Optimized strategy stack takes priority when wired.
        if self._strategy_adapter is not None:
            self._strategy_adapter.kill_switch_active = self.state.is_killed
            router_signal = self._strategy_adapter.push_bar(bar)
            if router_signal is not None:
                atr = bar.get("atr_14", bar["close"] * 0.02)
                lev = self.effective_leverage(
                    router_signal.confidence, bar["close"], atr,
                )
                if lev is not None:
                    router_signal.meta["leverage"] = round(lev, 1)
                    await self.on_signal(router_signal)
                return
        for setup_fn in (self.trend_follow, self.mean_revert, self.breakout):
            signal = setup_fn(bar)
            if signal is not None:
                atr = bar.get("atr_14", bar["close"] * 0.02)
                lev = self.effective_leverage(signal.confidence, bar["close"], atr)
                if lev is not None:
                    signal.meta["leverage"] = round(lev, 1)
                    await self.on_signal(signal)
                break

    async def on_signal(self, signal: Signal) -> OrderResult | None:
        """Route a signal to the venue, logging leverage decision."""
        lev = signal.meta.get("leverage", "?")
        logger.info(
            "%s signal: %s @ %.4f conf=%.1f lev=%sx",
            self.config.name, signal.type.value, signal.price, signal.confidence, lev,
        )
        if self._router is None:
            return None
        qty = self._size_from_signal(signal)
        if qty <= 0.0:
            logger.debug("%s signal skipped: qty=%.8f <= 0", self.config.name, qty)
            return None
        side, reduce_only = self._signal_to_order_side(signal.type)
        req = OrderRequest(
            symbol=self._venue_symbol,
            side=side,
            qty=qty,
            reduce_only=reduce_only,
        )
        try:
            result = await self._router.place_with_failover(req)
        except Exception as e:  # noqa: BLE001 - router logs & alerts internally
            logger.error("%s route failed: %s", self.config.name, e)
            return None
        if result.status is OrderStatus.REJECTED:
            logger.warning("%s order rejected: id=%s", self.config.name, result.order_id)
        return result

    # ── Sizing ──

    def _size_from_signal(self, signal: Signal) -> float:
        """Contract/coin count for a signal.

        Uses ``signal.size`` if the caller already sized it; otherwise derives
        notional from risk-per-trade and a stop distance (``meta['stop_distance']``
        or 2 * ATR fallback). Converts notional -> coin quantity via entry price.
        """
        if signal.size > 0.0:
            return float(signal.size)
        risk_usd = self.state.equity * (self.config.risk_per_trade_pct / 100.0)
        lev = float(signal.meta.get("leverage", 1.0))
        stop_distance = float(signal.meta.get("stop_distance", signal.price * 0.01))
        if stop_distance <= 0.0 or signal.price <= 0.0:
            return 0.0
        # Notional that risks `risk_usd` on a `stop_distance` USD move.
        # For linear perps 1 coin x stop_distance_USD = risk_usd -> coin = risk_usd / stop_distance
        base_coins = risk_usd / stop_distance
        # Leverage amplifies the size the router sends to the venue.
        coins = base_coins * max(lev, 1.0)
        # 4 dp rounding keeps the venue happy (Bybit min tick).
        return round(max(coins, 0.0), 4)

    @staticmethod
    def _signal_to_order_side(sig_type: SignalType) -> tuple[Side, bool]:
        if sig_type is SignalType.LONG:
            return Side.BUY, False
        if sig_type is SignalType.SHORT:
            return Side.SELL, False
        if sig_type is SignalType.CLOSE_LONG:
            return Side.SELL, True
        if sig_type is SignalType.CLOSE_SHORT:
            return Side.BUY, True
        return Side.BUY, False

    # ── Decision Logic ──

    def evaluate_entry(self, bar: dict[str, Any], confluence_score: float) -> bool:
        atr = bar.get("atr_14", bar.get("close", 1) * 0.02)
        lev = self.effective_leverage(confluence_score, bar.get("close", 0), atr)
        return lev is not None and self.check_risk()

    def evaluate_exit(self, position: Position) -> bool:
        risk_usd = self.config.risk_per_trade_pct / 100 * self.state.equity
        if position.unrealized_pnl <= -risk_usd:
            return True
        return position.unrealized_pnl >= 3.0 * risk_usd
