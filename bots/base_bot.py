"""EVOLUTIONARY TRADING ALGO -- Abstract base bot and shared types for the 6-bot fleet."""

from __future__ import annotations

import abc
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Tier(StrEnum):
    FUTURES = "FUTURES"
    SEED = "SEED"
    CASINO = "CASINO"


class MarginMode(StrEnum):
    CROSS = "cross"
    ISOLATED = "isolated"


class SignalType(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"
    GRID_ADD = "GRID_ADD"
    GRID_REMOVE = "GRID_REMOVE"


class RegimeType(StrEnum):
    TRENDING = "TRENDING"
    TRANSITION = "TRANSITION"
    RANGING = "RANGING"


class Signal(BaseModel):
    type: SignalType
    symbol: str
    price: float
    size: float = 0.0
    confidence: float = 0.0
    meta: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=datetime.utcnow)


class SweepResult(BaseModel):
    swept: bool
    direction: SignalType | None = None
    level: float = 0.0
    reclaim_confirmed: bool = False


class Position(BaseModel):
    symbol: str
    side: str
    entry_price: float
    size: float
    unrealized_pnl: float = 0.0
    opened_at: datetime = Field(default_factory=datetime.utcnow)


class Fill(BaseModel):
    symbol: str
    side: str
    price: float
    size: float
    fee: float = 0.0
    realized_pnl: float = 0.0
    risk_at_entry: float = 0.0
    ts: datetime = Field(default_factory=datetime.utcnow)


class BotConfig(BaseModel):
    name: str
    symbol: str
    tier: Tier
    baseline_usd: float
    starting_capital_usd: float
    max_leverage: float = 1.0
    risk_per_trade_pct: float = 1.0
    daily_loss_cap_pct: float = 2.5
    max_dd_kill_pct: float = 8.0
    margin_mode: MarginMode = MarginMode.CROSS


class BotState(BaseModel):
    equity: float = 0.0
    peak_equity: float = 0.0
    todays_pnl: float = 0.0
    open_positions: list[Position] = Field(default_factory=list)
    trades_today: int = 0
    is_killed: bool = False
    is_paused: bool = False


class BaseBot(abc.ABC):
    """Abstract base for every EVOLUTIONARY TRADING ALGO bot."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.state = BotState(equity=config.starting_capital_usd, peak_equity=config.starting_capital_usd)
        # Tier-4 #11 (2026-04-27): equity-ceiling injection point so the
        # portfolio rebalancer can dynamically allocate budget across
        # bots without touching their static config. None = no override.
        self._equity_ceiling_usd: float | None = None
        # Tier-3 #10 (2026-04-27): optional fleet-wide structured logger.
        # When set via attach_eta_logger(), every loggable event flows
        # through state/logs/eta.jsonl with bot=name attached.
        self._eta_logger: Any | None = None

    def set_equity_ceiling(self, usd: float | None) -> None:
        """Cap (or uncap) the bot's effective equity.

        Set by the portfolio rebalancer to redistribute budget across
        bots based on rolling Sharpe + drawdown brakes (see
        ``brain/portfolio_rebalancer_v2.py``). Pass ``None`` to remove
        the cap -- bot uses its full ``config.starting_capital_usd``.

        Sizing logic that respects this cap should call
        ``self.effective_equity()`` instead of ``self.state.equity``.
        """
        if usd is not None and usd <= 0:
            raise ValueError(f"equity ceiling must be positive or None, got {usd}")
        self._equity_ceiling_usd = float(usd) if usd is not None else None

    def effective_equity(self) -> float:
        """Bot equity after applying any portfolio-rebalancer ceiling.

        Returns ``min(state.equity, ceiling)`` when a ceiling is set,
        else ``state.equity`` unchanged. Bots that opt into the
        rebalancer should use this in their sizing math.
        """
        if self._equity_ceiling_usd is None:
            return self.state.equity
        return min(self.state.equity, self._equity_ceiling_usd)

    def attach_eta_logger(self, logger: Any) -> None:
        """Wire a fleet-wide ``EtaLogger`` (from ``obs.log_aggregator``).

        Idempotent: re-calling with the same logger is a no-op. Bots
        that opt in get one ``state/logs/eta.jsonl`` line per call to
        ``self.log()``. Bots that don't call this fall back to the
        plain ``logging.getLogger`` they already use.
        """
        self._eta_logger = logger

    @abc.abstractmethod
    async def start(self) -> None: ...
    @abc.abstractmethod
    async def stop(self) -> None: ...
    @abc.abstractmethod
    async def on_bar(self, bar: dict[str, Any]) -> None: ...
    @abc.abstractmethod
    async def on_signal(self, signal: Signal) -> None: ...
    @abc.abstractmethod
    def evaluate_entry(self, bar: dict[str, Any], confluence_score: float) -> bool: ...
    @abc.abstractmethod
    def evaluate_exit(self, position: Position) -> bool: ...

    def update_state(self, fill: Fill) -> None:
        self.state.equity += fill.realized_pnl - fill.fee
        self.state.todays_pnl += fill.realized_pnl - fill.fee
        self.state.trades_today += 1
        if self.state.equity > self.state.peak_equity:
            self.state.peak_equity = self.state.equity

    def check_risk(self) -> bool:
        """Return True if trading is permitted, False to halt."""
        if self.state.is_killed:
            return False
        daily_loss_pct = abs(self.state.todays_pnl) / self.config.starting_capital_usd * 100
        if self.state.todays_pnl < 0 and daily_loss_pct >= self.config.daily_loss_cap_pct:
            self.state.is_paused = True
            return False
        dd_pct = (self.state.peak_equity - self.state.equity) / self.state.peak_equity * 100
        if dd_pct >= self.config.max_dd_kill_pct:
            self.state.is_killed = True
            return False
        return True

    def sweep_check(self, bar: dict[str, Any], levels: list[float]) -> SweepResult | None:
        """Detect liquidity sweep: wick beyond level then close inside."""
        high, low, close = bar["high"], bar["low"], bar["close"]
        for lvl in levels:
            if high > lvl > close:
                return SweepResult(swept=True, direction=SignalType.SHORT, level=lvl, reclaim_confirmed=close < lvl)
            if low < lvl < close:
                return SweepResult(swept=True, direction=SignalType.LONG, level=lvl, reclaim_confirmed=close > lvl)
        return None
