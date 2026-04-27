"""EVOLUTIONARY TRADING ALGO -- Abstract base bot and shared types for the 6-bot fleet."""

from __future__ import annotations

import abc
from collections import deque
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

#: Default rolling sage-bar buffer length. Sage's MarketContext needs
#: at least 30 bars; we keep 200 by default so the multi-timeframe
#: schools (Elliott / Wyckoff / dow-theory) get enough lookback.
DEFAULT_SAGE_BAR_BUFFER: int = 200


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
        # Wave-6 pre-live (2026-04-27): rolling sage-bar buffer.
        # Bots that want JARVIS to consult the multi-school sage on each
        # ORDER_PLACE call should:
        #   1. Call ``self.observe_bar_for_sage(bar)`` from their on_bar
        #   2. Either:
        #      a. Call ``self.run_pre_flight(...)`` -- bars auto-attach, OR
        #      b. Pass ``sage_bars=self.recent_sage_bars()`` in their
        #         ``self._ask_jarvis(ActionType.ORDER_PLACE, ...)`` payload
        # When the buffer is empty, sage gracefully falls back to v17.
        self._sage_bar_history: deque[dict[str, Any]] = deque(
            maxlen=DEFAULT_SAGE_BAR_BUFFER,
        )
        # Wave-6 (2026-04-27): per-bot last-sage-report cache so the
        # fill-close path can feed each school's verdict into the
        # edge_tracker once realized R is known. Keyed by symbol.
        self._last_sage_reports: dict[str, Any] = {}
        # 2026-04-27 risk-sage hardening: optional fleet-wide
        # FleetRiskGate (from safety/fleet_risk_gate.py). When set
        # via attach_fleet_risk_gate(), record_fill_outcome converts
        # the realized R-multiple into USD and registers it with the
        # gate so the fleet-aggregate daily-loss budget is enforced.
        # None = bot opts out (paper / unit-test paths that don't
        # need cross-bot aggregation).
        self._fleet_risk_gate: Any | None = None
        # bot_id used to attribute PnL to the registry row when the
        # gate is wired. Defaults to the bot's config.name; can be
        # overridden via attach_fleet_risk_gate(..., bot_id=...) so
        # strategy-variants (mnq_futures vs mnq_futures_sage) record
        # to distinct registry rows.
        self._fleet_risk_bot_id: str | None = None

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

    def attach_eta_logger(self, logger: Any) -> None:  # noqa: ANN401 -- duck-typed EtaLogger
        """Wire a fleet-wide ``EtaLogger`` (from ``obs.log_aggregator``).

        Idempotent: re-calling with the same logger is a no-op. Bots
        that opt in get one ``state/logs/eta.jsonl`` line per call to
        ``self.log()``. Bots that don't call this fall back to the
        plain ``logging.getLogger`` they already use.
        """
        self._eta_logger = logger

    def attach_fleet_risk_gate(
        self,
        gate: Any,  # noqa: ANN401 -- duck-typed FleetRiskGate
        *,
        bot_id: str | None = None,
    ) -> None:
        """Wire a fleet-wide ``FleetRiskGate`` (from ``safety.fleet_risk_gate``).

        Once attached, every fill processed by ``record_fill_outcome``
        converts its realized R-multiple to USD (using the bot's
        risk-per-trade budget) and registers the delta with the gate
        via ``gate.record_pnl(bot_id, delta_usd)``. The gate's
        aggregate trip threshold is enforced at order-submit time,
        not here -- this method only feeds the running total.

        ``bot_id`` defaults to ``self.config.name``. Override when a
        strategy variant should attribute PnL to a separate registry
        row (e.g. ``"mnq_futures_sage"`` vs ``"mnq_futures"``).

        Idempotent: re-calling with the same gate is a no-op. Pass
        ``gate=None`` to detach (paper / test resets).
        """
        self._fleet_risk_gate = gate
        self._fleet_risk_bot_id = bot_id or getattr(self.config, "name", None)

    # --- Wave-6 sage bar history (opt-in) ----------------------------

    def observe_bar_for_sage(self, bar: dict[str, Any]) -> None:
        """Append a bar to the rolling sage-bar buffer.

        Bots opt into multi-school sage modulation by calling this in
        their ``on_bar``. The buffer is bounded; older bars age out.
        The bar dict must carry at least ``open / high / low / close``;
        ``volume`` and ``ts`` are optional but recommended (sage uses
        them when present).

        No-op cost when sage is not in the live path -- the buffer just
        sits there. Cost when sage IS live is one append per bar.
        """
        if not isinstance(bar, dict):
            return
        # Defensive copy so subsequent caller mutations don't poison the
        # cached bars (sage runs ASYNC of bar arrival in some bots).
        self._sage_bar_history.append(dict(bar))

    def recent_sage_bars(self, n: int | None = None) -> list[dict[str, Any]]:
        """Snapshot of the most recent ``n`` bars (default: full buffer).

        Returns a list (not the deque itself) so callers can pass it
        through pydantic-typed payloads safely. Returns an empty list
        when the bot hasn't been observing bars.
        """
        if not self._sage_bar_history:
            return []
        if n is None or n >= len(self._sage_bar_history):
            return list(self._sage_bar_history)
        return list(self._sage_bar_history)[-n:]

    def cache_sage_report(self, symbol: str, report: Any) -> None:  # noqa: ANN401 -- duck-typed SageReport
        """Stash the sage report from the most recent ORDER_PLACE consult.

        Called by the v22 modulator (via the audit path) so when this
        bot's fill closes, ``record_fill_outcome`` can pull the per-school
        bias and feed each one into ``edge_tracker.observe()`` with the
        realized R-multiple.
        """
        if symbol:
            self._last_sage_reports[symbol] = report

    def pop_cached_sage_report(self, symbol: str) -> Any | None:  # noqa: ANN401 -- duck-typed SageReport
        """Drain the last sage report for ``symbol`` (one-shot per fill).

        Falls back to the global module-level cache populated by v22 when
        the bot itself didn't stash one. This is the normal path: v22
        runs inside the JARVIS evaluation, doesn't have a bot reference,
        so it writes to the global cache; the bot reads back here.
        """
        local = self._last_sage_reports.pop(symbol, None)
        if local is not None:
            return local
        # Fallback to the v22-populated global cache
        try:
            from eta_engine.brain.jarvis_v3.sage.last_report_cache import pop_last
            return pop_last(symbol)
        except Exception:  # noqa: BLE001
            return None

    def run_pre_flight(
        self,
        *,
        symbol: str,
        side: str,
        confluence: float,
        fleet_positions: dict[str, float] | None = None,
        rationale: str = "",
        extra_payload: dict[str, Any] | None = None,
    ) -> Any:  # noqa: ANN401 -- duck-typed PreflightDecision
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
        symbol: str | None = None,
    ) -> None:
        """Tier-1 wave-3 (2026-04-27): record fill outcome for kaizen + learning.

        Closes the feedback loop end-to-end. Three effects:

          1. Writes a journal event (via ``record_fill_with_realized_r``)
             with ``metadata['realized_r']`` populated -- the kaizen
             synthesizer reads this to ground went_well/went_poorly in
             realized R-multiples instead of just gate firings.

          2. Calls ``observe_fill_for_learning(feature_bucket, r_multiple)``
             so an attached ``OnlineUpdater`` (if any) gets the data point.

          3. Wave-6 (2026-04-27): if a sage report was cached at entry
             (via ``cache_sage_report``), feeds each per-school verdict
             into the EdgeTracker with the realized R. This is what makes
             the per-school weight modifier earn / lose say over time.

        Backward-compatible: when no journal, no online updater, AND no
        cached sage report, this is a no-op.
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

        # Effect 2b (2026-04-27 risk-sage): aggregate PnL into the
        # FleetRiskGate so the fleet-wide daily-loss budget is fed
        # by every closing fill. The gate is process-wide; trip
        # decisions happen at order-submit time, not here.
        # Defensive getattr so duck-typed test stubs (which don't call
        # BaseBot.__init__) don't crash the fill loop.
        gate = getattr(self, "_fleet_risk_gate", None)
        if gate is not None:
            try:
                # Convert R-multiple to USD using risk-per-trade $.
                # Falls back gracefully if the bot's config doesn't
                # carry both required fields (some duck-typed test
                # configs only set name/symbol).
                cfg = self.config
                risk_pct = float(getattr(cfg, "risk_per_trade_pct", 1.0))
                start_usd = float(getattr(cfg, "starting_capital_usd", 0.0))
                # risk_per_trade_pct is in *percent* (e.g. 1.0 = 1%),
                # not a fraction — divide by 100 to get USD risk.
                risk_usd = (risk_pct / 100.0) * start_usd
                if risk_usd > 0.0:
                    delta_usd = float(r_multiple) * risk_usd
                    bot_id = getattr(self, "_fleet_risk_bot_id", None) or getattr(cfg, "name", "unknown")
                    gate.record_pnl(bot_id, delta_usd)
            except Exception as exc:  # noqa: BLE001 -- never crash the fill loop
                import logging
                logging.getLogger(__name__).warning(
                    "FleetRiskGate.record_pnl failed (non-fatal): %s", exc,
                )

        # Effect 3 (wave-6): sage edge-tracker feedback. Only fires when
        # the bot stashed a sage report at entry via cache_sage_report().
        # Lazy import + try/except so the trading loop never dies if the
        # tracker has a disk problem. Defensive symbol lookup so duck-
        # typed config objects (used in some tests / micro-bots) don't
        # crash the fill loop.
        sage_symbol = symbol or getattr(self.config, "symbol", "") or ""
        sage_report = self.pop_cached_sage_report(sage_symbol) if sage_symbol else None
        if sage_report is not None:
            try:
                from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker
                tracker = default_tracker()
                per_school = getattr(sage_report, "per_school", None) or {}
                # Recover the entry side from the cached report. SageReport
                # was built with ``ctx.side`` so each verdict knows what
                # direction the bot took.
                entry_side_str = ""
                for verdict in per_school.values():
                    # Every verdict carries aligned_with_entry; back out the
                    # entry side from one verdict (long if bias==long &&
                    # aligned, short if bias==short && aligned, etc.)
                    if verdict.aligned_with_entry:
                        entry_side_str = getattr(verdict.bias, "value", "")
                        break
                if not entry_side_str:
                    # Fallback: realized R can hint at side via convention,
                    # but we can't recover it cleanly. Default "long".
                    entry_side_str = "long"
                for school_name, verdict in per_school.items():
                    bias_value = getattr(verdict.bias, "value", str(verdict.bias))
                    tracker.observe(
                        school=school_name,
                        school_bias=bias_value,
                        entry_side=entry_side_str,
                        realized_r=float(r_multiple),
                    )
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning(
                    "edge_tracker observe failed (non-fatal): %s", exc,
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
