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

    def run_pre_flight(
        self,
        *,
        symbol: str,
        side: str,
        confluence: float,
        fleet_positions: dict[str, float] | None = None,
        rationale: str = "",
        extra_payload: dict[str, Any] | None = None,
    ) -> Any:
        """Tier-1 wave-3 (2026-04-27): one-call pre-flight composer.

        Combines the cross-bot correlation throttle and JARVIS gate via
        ``brain/jarvis_pre_flight.bot_pre_flight``. Returns a
        ``PreflightDecision`` with .allowed / .size_cap_mult / .reason
        / .reason_code / .binding fields.

        Bots opt in by calling this in their ``on_signal`` flow instead
        of (or before) calling ``self._ask_jarvis`` directly. When
        no JARVIS is wired (legacy mode), the underlying composer
        passes through with correlation cap applied -- so this method
        is safe to call from any bot.

        Example::

            decision = self.run_pre_flight(
                symbol=signal.symbol,
                side=signal.type.value,
                confluence=signal.confidence,
                fleet_positions=fleet.positions_by_symbol(),
            )
            if not decision.allowed:
                self._log_blocked(decision.reason_code, decision.reason)
                return
            qty = base_qty * decision.size_cap_mult
            ...
        """
        from eta_engine.brain.jarvis_pre_flight import bot_pre_flight
        return bot_pre_flight(
            bot=self,
            symbol=symbol,
            side=side,
            confluence=confluence,
            fleet_positions=fleet_positions or {},
            rationale=rationale,
            extra_payload=extra_payload,
        )

    def record_fill_outcome(
        self,
        *,
        intent: str,
        r_multiple: float,
        feature_bucket: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Tier-1 wave-3 (2026-04-27): record fill outcome for kaizen + learning.

        Closes the feedback loop end-to-end. Two effects:

          1. Writes a journal event (via ``record_fill_with_realized_r``)
             with ``metadata['realized_r']`` populated -- the kaizen
             synthesizer reads this to ground went_well/went_poorly in
             realized R-multiples instead of just gate firings.

          2. Calls ``observe_fill_for_learning(feature_bucket, r_multiple)``
             so an attached ``OnlineUpdater`` (if any) gets the data point.

        Backward-compatible: when no journal is attached AND no online
        updater is attached, this is a no-op.

        Bots that have already been doing per-bot journaling can ALSO
        call this -- the events compose (no double-counting since
        kaizen synthesizer only reads metadata['realized_r']).
        """
        from eta_engine.brain.jarvis_pre_flight import record_fill_with_realized_r

        # Effect 1: journal event with realized_r metadata
        journal = getattr(self, "_journal", None)
        if journal is not None:
            try:
                record_fill_with_realized_r(
                    journal,
                    intent=intent,
                    r_multiple=r_multiple,
                    bot_name=self.config.name,
                    extra=extra,
                )
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning(
                    "record_fill_with_realized_r failed (non-fatal): %s", exc,
                )

        # Effect 2: online learning update (if attached)
        if feature_bucket is not None:
            self.observe_fill_for_learning(
                feature_bucket=feature_bucket,
                r_multiple=r_multiple,
            )

    def observe_fill_for_learning(
        self,
        *,
        feature_bucket: str,
        r_multiple: float,
    ) -> None:
        """Tier-4 #12 (2026-04-27): online-learning hook for fill outcomes.

        Bots that opt into the rebalancer / online-learning pipeline call
        this on every closed trade. ``feature_bucket`` is the strategy-
        specific descriptor (e.g. ``"confluence_8"``, ``"regime_trend"``).
        ``r_multiple`` is the realized R-multiple of the trade (+1.0 = won
        a 1R trade, -1.0 = stopped at 1R, etc.).

        Backward-compatible: if the bot doesn't have an ``OnlineUpdater``
        attached (default), this is a no-op. To opt in::

            from eta_engine.brain.online_learning import OnlineUpdater
            self._online_updater = OnlineUpdater(bot_name=self.config.name)

        Then in ``on_fill``, after computing R::

            self.observe_fill_for_learning(
                feature_bucket=f"confluence_{int(c)}",
                r_multiple=R,
            )
        """
        upd = getattr(self, "_online_updater", None)
        if upd is None:
            return
        try:
            upd.observe(feature_bucket=feature_bucket, r_multiple=r_multiple)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "online_updater.observe failed (non-fatal): %s", exc,
            )

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
