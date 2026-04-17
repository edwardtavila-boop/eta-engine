"""Crypto Seed Bot -- SEED tier, geometric grid + directional overlay.

BTCUSDT perpetual. Grid harvests chop; overlay fires on confluence > 7.

Injectable dependencies
-----------------------
* ``router`` — same :class:`SmartRouter`-compatible interface as the other bots.
  When supplied, the directional overlay routes through the router. The grid
  fills are returned by ``manage_grid`` and can be drained into ``router`` by
  the orchestrator that owns the bot (we don't route every grid line here;
  doing so would spam the router 40x per bar).
* ``venue_symbol`` — exchange contract symbol (defaults to ``config.symbol``).
* ``initial_grid_bounds`` — pre-computed ``(high, low)`` to seed the grid at
  construction. If omitted, the grid is empty until ``init_grid`` is called.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, Field

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
SEED_CONFIG = BotConfig(
    name="Crypto-Seed", symbol="BTCUSDT", tier=Tier.SEED, baseline_usd=2000.0,
    starting_capital_usd=2000.0, max_leverage=3.0, risk_per_trade_pct=0.5,
    daily_loss_cap_pct=3.0, max_dd_kill_pct=10.0, margin_mode=MarginMode.ISOLATED,
)


class GridConfig(BaseModel):
    n_levels: int = 40
    spacing: str = "geometric"  # "geometric" | "arithmetic"
    price_high: float = 0.0
    price_low: float = 0.0
    capital_per_level: float = 0.0


class GridOrder(BaseModel):
    level_idx: int
    price: float
    side: str  # "BUY" | "SELL"
    size: float
    is_active: bool = True


class GridState(BaseModel):
    levels: list[float] = Field(default_factory=list)
    active_orders: list[GridOrder] = Field(default_factory=list)
    filled_buys: int = 0
    filled_sells: int = 0


class _Router(Protocol):
    async def place_with_failover(self, req: OrderRequest) -> OrderResult: ...


class CryptoSeedBot(BaseBot):
    """BTC grid bot with directional overlay on high confluence."""

    def __init__(
        self,
        config: BotConfig | None = None,
        *,
        router: _Router | None = None,
        venue_symbol: str | None = None,
        initial_grid_bounds: tuple[float, float] | None = None,
        strategy_adapter: RouterAdapter | None = None,
    ) -> None:
        super().__init__(config or SEED_CONFIG)
        self.grid_config = GridConfig()
        self.grid_state = GridState()
        self._router = router
        self._venue_symbol = venue_symbol or self.config.symbol
        self._strategy_adapter = strategy_adapter
        if initial_grid_bounds is not None:
            high, low = initial_grid_bounds
            self.init_grid(high, low)

    # ── Lifecycle ──

    async def start(self) -> None:
        logger.info(
            "Crypto Seed bot starting | capital=$%.2f symbol=%s grid_levels=%d router=%s",
            self.config.starting_capital_usd, self._venue_symbol,
            len(self.grid_state.levels),
            "yes" if self._router is not None else "no",
        )

    async def stop(self) -> None:
        logger.info("Crypto Seed bot stopping | equity=$%.2f", self.state.equity)
        # Mark all grid orders inactive so the orchestrator knows to cancel on-exchange.
        for order in self.grid_state.active_orders:
            order.is_active = False

    # ── Grid Math ──

    @staticmethod
    def calculate_grid_levels(high: float, low: float, n: int) -> list[float]:
        """Geometric grid: ratio r = (high/low)^(1/n), levels = low * r^i."""
        if high <= low or n <= 0:
            return []
        r = (high / low) ** (1.0 / n)
        return [low * (r ** i) for i in range(n + 1)]

    def init_grid(self, price_high: float, price_low: float) -> None:
        """Set grid bounds and compute levels + per-level capital."""
        self.grid_config.price_high = price_high
        self.grid_config.price_low = price_low
        levels = self.calculate_grid_levels(price_high, price_low, self.grid_config.n_levels)
        self.grid_state.levels = levels
        usable = self.state.equity * 0.90  # 10% reserve
        self.grid_config.capital_per_level = usable / max(len(levels), 1)

    def manage_grid(self, current_price: float, grid_state: GridState) -> list[GridOrder]:
        """Evaluate grid vs current price, return orders to place/cancel."""
        orders: list[GridOrder] = []
        for i, lvl in enumerate(grid_state.levels):
            size = self.grid_config.capital_per_level / lvl if lvl > 0 else 0.0
            if lvl < current_price:
                orders.append(GridOrder(level_idx=i, price=lvl, side="BUY", size=size))
            else:
                orders.append(GridOrder(level_idx=i, price=lvl, side="SELL", size=size))
        return orders

    # ── Directional Overlay ──

    def directional_overlay(self, bar: dict[str, Any], confluence: float) -> Signal | None:
        """Directional trade on top of grid when confluence > 7."""
        if confluence <= 7.0:
            return None
        ema_fast: float = bar.get("ema_9", 0.0)
        ema_slow: float = bar.get("ema_21", 0.0)
        if ema_fast == 0.0 or ema_slow == 0.0:
            return None
        if ema_fast > ema_slow:
            return Signal(type=SignalType.LONG, symbol=self.config.symbol, price=bar["close"], confidence=confluence)
        if ema_fast < ema_slow:
            return Signal(type=SignalType.SHORT, symbol=self.config.symbol, price=bar["close"], confidence=confluence)
        return None

    # ── Market Events ──

    async def on_bar(self, bar: dict[str, Any]) -> None:
        if not self.check_risk():
            return
        grid_orders = self.manage_grid(bar["close"], self.grid_state)
        # Orchestrator drains grid_orders at its own cadence (see scripts/run_eta_live).
        self.grid_state.active_orders = grid_orders
        # AI-Optimized strategy stack takes priority when wired.
        if self._strategy_adapter is not None:
            self._strategy_adapter.kill_switch_active = self.state.is_killed
            router_signal = self._strategy_adapter.push_bar(bar)
            if router_signal is not None:
                await self.on_signal(router_signal)
                return
        confluence: float = bar.get("confluence_score", 0.0)
        signal = self.directional_overlay(bar, confluence)
        if signal:
            await self.on_signal(signal)

    async def on_signal(self, signal: Signal) -> OrderResult | None:
        """Route the directional-overlay signal. Grid fills are drained separately."""
        logger.info("Seed signal: %s @ %.2f conf=%.1f", signal.type.value, signal.price, signal.confidence)
        if self._router is None:
            return None
        # Seed-tier uses a fixed 1% notional of equity per directional trade — stop
        # sizing is managed by the 1.5R exit in evaluate_exit rather than a stop_distance.
        notional = self.state.equity * (self.config.risk_per_trade_pct / 100.0)
        if signal.price <= 0.0 or notional <= 0.0:
            return None
        qty = round(notional / signal.price, 6)
        if qty <= 0.0:
            return None
        side = Side.BUY if signal.type is SignalType.LONG else Side.SELL
        req = OrderRequest(symbol=self._venue_symbol, side=side, qty=qty)
        try:
            result = await self._router.place_with_failover(req)
        except Exception as e:  # noqa: BLE001
            logger.error("Seed route failed: %s", e)
            return None
        if result.status is OrderStatus.REJECTED:
            logger.warning("Seed order rejected: id=%s", result.order_id)
        return result

    # ── Decision Logic ──

    def evaluate_entry(self, bar: dict[str, Any], confluence_score: float) -> bool:
        return confluence_score > 7.0 and self.check_risk()

    def evaluate_exit(self, position: Position) -> bool:
        risk_usd = self.config.risk_per_trade_pct / 100 * self.state.equity
        if position.unrealized_pnl <= -risk_usd:
            return True
        return position.unrealized_pnl >= 1.5 * risk_usd
