"""APEX PREDATOR -- Abstract base bot and shared types for the 6-bot fleet."""
from __future__ import annotations

import abc
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Tier(str, Enum):
    FUTURES = "FUTURES"; SEED = "SEED"; CASINO = "CASINO"

class MarginMode(str, Enum):
    CROSS = "cross"; ISOLATED = "isolated"

class SignalType(str, Enum):
    LONG = "LONG"; SHORT = "SHORT"; CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"; GRID_ADD = "GRID_ADD"; GRID_REMOVE = "GRID_REMOVE"

class RegimeType(str, Enum):
    TRENDING = "TRENDING"; TRANSITION = "TRANSITION"; RANGING = "RANGING"

class Signal(BaseModel):
    type: SignalType; symbol: str; price: float; size: float = 0.0
    confidence: float = 0.0; meta: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=datetime.utcnow)

class SweepResult(BaseModel):
    swept: bool; direction: SignalType | None = None
    level: float = 0.0; reclaim_confirmed: bool = False

class Position(BaseModel):
    symbol: str; side: str; entry_price: float; size: float
    unrealized_pnl: float = 0.0; opened_at: datetime = Field(default_factory=datetime.utcnow)

class Fill(BaseModel):
    symbol: str; side: str; price: float; size: float
    fee: float = 0.0; realized_pnl: float = 0.0
    risk_at_entry: float = 0.0
    ts: datetime = Field(default_factory=datetime.utcnow)

class BotConfig(BaseModel):
    name: str; symbol: str; tier: Tier; baseline_usd: float; starting_capital_usd: float
    max_leverage: float = 1.0; risk_per_trade_pct: float = 1.0
    daily_loss_cap_pct: float = 2.5; max_dd_kill_pct: float = 8.0
    margin_mode: MarginMode = MarginMode.CROSS

class BotState(BaseModel):
    equity: float = 0.0; peak_equity: float = 0.0; todays_pnl: float = 0.0
    open_positions: list[Position] = Field(default_factory=list)
    trades_today: int = 0; is_killed: bool = False; is_paused: bool = False

class BaseBot(abc.ABC):
    """Abstract base for every APEX PREDATOR bot."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.state = BotState(equity=config.starting_capital_usd, peak_equity=config.starting_capital_usd)

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
