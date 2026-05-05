"""ETH Perp Bot -- CASINO tier, leverage-gated by confluence.

Liquidation-proof: max_lev = price / (3 * ATR14_5m * 1.20 + price * 0.005).

Injectable dependencies
-----------------------
Same contract as :class:`eta_engine.bots.mnq.bot.MnqBot`:

* ``router`` -- anything exposing ``async place_with_failover(OrderRequest)``.
  When supplied, ``on_signal`` routes market orders to the venue with the
  ``leverage`` recorded in ``meta``. The adapter layer is expected to send
  ``set_leverage(symbol, lev)`` before the order (see ``venues.bybit``).
* ``jarvis`` -- optional :class:`JarvisAdmin`. When supplied, every
  risk-adding action (STRATEGY_DEPLOY on start, ORDER_PLACE on every
  outgoing signal) gates through
  :meth:`JarvisAdmin.request_approval` BEFORE the venue sees the
  order. DENIED / DEFERRED verdicts refuse the action; CONDITIONAL
  verdicts carry a ``size_cap_mult`` that scales the qty AND the
  effective leverage proportionally. When ``None`` the bot runs
  without JARVIS oversight (the pre-v0.1.58 path, still used by
  unit tests).
* ``journal`` -- optional :class:`DecisionJournal` for per-decision
  audit.
* ``provide_ctx`` -- optional zero-arg callable returning a fresh
  :class:`JarvisContext`. When ``None`` and ``jarvis`` is set, the
  caller MUST have wired an engine into the ``JarvisAdmin`` so it
  can self-tick.
* ``venue_symbol`` -- exchange-specific contract symbol. Defaults to ``config.symbol``.

Subclasses (SOL, XRP) inherit the JARVIS wiring AND the router path --
they only override the per-instrument setup thresholds and leverage
math. SOL / XRP bots automatically carry SUBSYSTEM = BOT_SOL_PERP /
BOT_XRP_PERP for audit distinction.

JARVIS takeover
---------------
Supplying ``jarvis=JarvisAdmin(...)`` puts the ETH perp bot in "takeover
mode" -- JARVIS gates every order. In this mode:

* ``start()`` requests :attr:`ActionType.STRATEGY_DEPLOY` with
  ``overnight_explicit=True`` so CRYPTO_24_7_BOTS policy fires; a
  refusal pauses the bot.
* ``on_signal()`` requests :attr:`ActionType.ORDER_PLACE` before each
  route on LONG/SHORT entries. Refused -> no order. CONDITIONAL ->
  coin qty scaled by ``size_cap_mult``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from eta_engine.bots.base_bot import (
    BaseBot,
    BotConfig,
    Fill,
    MarginMode,
    Position,
    RegimeType,
    Signal,
    SignalType,
    Tier,
)
from eta_engine.brain.jarvis_admin import (
    ActionType,
    JarvisAdmin,
    SubsystemId,
)
from eta_engine.brain.jarvis_gate import (
    ask_jarvis,
    pick_llm_tier,
    record_gate_event,
)
from eta_engine.obs.decision_journal import Actor, DecisionJournal, Outcome
from eta_engine.venues.base import OrderRequest, OrderResult, OrderStatus, Side

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.bots.retrospective_adapter import ActiveEntry
    from eta_engine.brain.jarvis_context import JarvisContext
    from eta_engine.brain.model_policy import ModelTier, TaskCategory
    from eta_engine.strategies.adaptive_sizing import RegimeLabel
    from eta_engine.strategies.engine_adapter import RouterAdapter
    from eta_engine.strategies.models import StrategyId
    from eta_engine.strategies.retrospective import RetrospectiveReport
    from eta_engine.strategies.retrospective_wiring import (
        RetrospectiveManager,
    )


# ETH uses a lower trending ADX threshold than MNQ (25 vs 30) to
# match the perp-market's higher volatility baseline.
_ADX_TREND_ETH: float = 25.0
_ADX_TRANSITION_ETH: float = 18.0

logger = logging.getLogger(__name__)
ETH_CONFIG = BotConfig(
    name="ETH-Perp",
    symbol="ETHUSDT",
    tier=Tier.CASINO,
    baseline_usd=3000.0,
    starting_capital_usd=3000.0,
    max_leverage=75.0,
    risk_per_trade_pct=3.0,
    daily_loss_cap_pct=6.0,
    max_dd_kill_pct=20.0,
    margin_mode=MarginMode.ISOLATED,
)
_LEV_TIERS: list[tuple[float, float]] = [(9.0, 75.0), (7.0, 20.0), (5.0, 10.0)]
_LEV_MIN_CONFLUENCE: float = 5.0


class _Router(Protocol):
    async def place_with_failover(
        self,
        req: OrderRequest,
        *,
        urgency: str = "normal",
    ) -> OrderResult: ...


class EthPerpBot(BaseBot):
    """ETH perp bot -- 3 directional setups, liquidation-proof leverage, router-backed.

    JARVIS-ready: pass ``jarvis=JarvisAdmin(...)`` and every
    ORDER_PLACE / STRATEGY_DEPLOY goes through JARVIS before the
    venue sees it. Subclasses (SOL, XRP) inherit this behavior; they
    only override ``SUBSYSTEM`` and instrument-specific tuning.
    """

    # Exposed so subclasses (SOL, XRP) can replace the leverage grid in one line.
    LEV_TIERS: list[tuple[float, float]] = _LEV_TIERS
    LEV_MIN_CONFLUENCE: float = _LEV_MIN_CONFLUENCE

    # Subsystem identity -- stable across the audit log. Subclasses
    # override with BOT_SOL_PERP / BOT_XRP_PERP.
    SUBSYSTEM: SubsystemId = SubsystemId.BOT_ETH_PERP

    def __init__(
        self,
        config: BotConfig | None = None,
        *,
        router: _Router | None = None,
        jarvis: JarvisAdmin | None = None,
        journal: DecisionJournal | None = None,
        provide_ctx: Callable[[], JarvisContext] | None = None,
        venue_symbol: str | None = None,
        strategy_adapter: RouterAdapter | None = None,
        auto_wire_ai_strategies: bool = False,
        ai_strategy_config: dict[str, Any] | None = None,
        retrospective_manager: RetrospectiveManager | None = None,
        auto_wire_retrospective: bool = False,
        retrospective_config: dict[str, Any] | None = None,
        default_retrospective_strategy: StrategyId | None = None,
    ) -> None:
        super().__init__(config or ETH_CONFIG)
        self._router = router
        self._jarvis = jarvis
        self._journal = journal
        self._provide_ctx = provide_ctx
        self._venue_symbol = venue_symbol or self.config.symbol
        self._strategy_adapter = strategy_adapter
        self._auto_wire_ai_strategies = auto_wire_ai_strategies
        self._ai_strategy_config: dict[str, Any] = dict(ai_strategy_config) if ai_strategy_config else {}
        self._retrospective_manager = retrospective_manager
        self._auto_wire_retrospective = auto_wire_retrospective
        self._retrospective_config: dict[str, Any] = dict(retrospective_config) if retrospective_config else {}
        self._default_retrospective_strategy = default_retrospective_strategy
        # v0.1.50: symbol -> ActiveEntry. See MnqBot for semantics.
        self._active_entries: dict[str, ActiveEntry] = {}

    # ── JARVIS gating helpers ──

    def _ask_jarvis(
        self,
        action: ActionType,
        **payload: Any,  # noqa: ANN401 -- payload is intentionally untyped
    ) -> tuple[bool, float | None, str]:
        """Gate a risk-adding action through JARVIS.

        Crypto bots are 24/7 so we always pass ``overnight_explicit=True``
        -- the admin policy uses CRYPTO_24_7_BOTS to whitelist overnight
        operation for this subsystem.

        When ``self._jarvis`` is ``None`` (legacy / test mode) this
        returns ``(True, None, "no_jarvis")`` so callers behave
        exactly as they did pre-v0.1.58.
        """
        if self._jarvis is None:
            return True, None, "no_jarvis"
        payload.setdefault("overnight_explicit", True)
        return ask_jarvis(
            self._jarvis,
            subsystem=self.SUBSYSTEM,
            action=action,
            rationale=payload.pop("rationale", ""),
            provide_ctx=self._provide_ctx,
            log_name=self.config.name,
            **payload,
        )

    def _record_event(
        self,
        *,
        intent: str,
        rationale: str = "",
        outcome: Outcome = Outcome.NOTED,
        **metadata: Any,  # noqa: ANN401 -- journal payloads are intentionally flexible
    ) -> None:
        """Append one journal event. No-op without a journal."""
        record_gate_event(
            self._journal,
            actor=Actor.TRADE_ENGINE,
            intent=intent,
            rationale=rationale,
            outcome=outcome,
            log_name=self.config.name,
            **metadata,
        )

    def pick_model_tier(
        self,
        category: TaskCategory,
        *,
        rationale: str = "",
    ) -> ModelTier:
        """Ask JARVIS which model tier to use for a given task."""
        if self._jarvis is None:
            from eta_engine.brain.model_policy import ModelTier as _ModelTier

            return _ModelTier.SONNET
        return pick_llm_tier(
            self._jarvis,
            subsystem=self.SUBSYSTEM,
            category=category,
            rationale=rationale,
        )

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

    def _prepare_signal_for_routing(
        self,
        signal: Signal,
        bar: dict[str, Any],
        regime: RegimeType,
    ) -> Signal:
        """Hook for subclasses that want to transform a signal before routing.

        ETH and XRP pass signals through unchanged. SOL uses this hook to
        invert directional bias in ranging regimes without duplicating the
        rest of the order routing path.
        """
        return signal

    # ── Lifecycle ──

    async def start(self) -> None:
        # JARVIS STRATEGY_DEPLOY gate -- refused under kill / stand-aside.
        # Crypto 24/7 so we mark overnight_explicit=True.
        allowed, _cap, code = self._ask_jarvis(
            ActionType.STRATEGY_DEPLOY,
            rationale=f"arming {self.config.name}",
            mode="perp_casino",
        )
        if not allowed:
            logger.warning("%s refused to start: %s", self.config.name, code)
            self._record_event(
                intent=f"{self.config.symbol.lower()}_start_blocked",
                rationale=f"jarvis refused STRATEGY_DEPLOY: {code}",
                outcome=Outcome.BLOCKED,
            )
            self.state.is_paused = True
            return

        if self._auto_wire_ai_strategies and self._strategy_adapter is None:
            from eta_engine.strategies.live_adapter import (
                build_live_adapter,
            )

            # ETH perp symbol is ETHUSDT; the strategy layer keys off
            # the asset prefix (ETH). The factory will upper-case it.
            adapter_asset = self.config.symbol.replace("USDT", "") or (self.config.symbol)
            self._strategy_adapter = build_live_adapter(
                adapter_asset,
                **self._ai_strategy_config,
            )
            logger.info(
                "%s auto-wired AI-Optimized strategy adapter (asset=%s, scheduler=on)",
                self.config.name,
                adapter_asset,
            )
        # Auto-wire the v0.1.48 retrospective loop if requested.
        if self._auto_wire_retrospective and self._retrospective_manager is None:
            from eta_engine.strategies.retrospective_wiring import (
                RetrospectiveManager,
            )

            self._retrospective_manager = RetrospectiveManager(
                starting_equity=self.config.starting_capital_usd,
                **self._retrospective_config,
            )
            logger.info(
                "%s auto-wired RetrospectiveManager (starting_equity=$%.2f)",
                self.config.name,
                self.config.starting_capital_usd,
            )
        logger.info(
            "%s starting | capital=$%.2f symbol=%s router=%s jarvis=%s retrospective=%s",
            self.config.name,
            self.config.starting_capital_usd,
            self._venue_symbol,
            "yes" if self._router is not None else "no",
            "yes" if self._jarvis is not None else "no",
            "yes" if self._retrospective_manager is not None else "no",
        )
        self._record_event(
            intent=f"{self.config.symbol.lower()}_start",
            rationale="jarvis approved STRATEGY_DEPLOY" if self._jarvis else "no_jarvis",
            outcome=Outcome.EXECUTED,
            symbol=self._venue_symbol,
            router="yes" if self._router is not None else "no",
            jarvis="yes" if self._jarvis is not None else "no",
        )

    async def stop(self) -> None:
        logger.info("%s stopping | equity=$%.2f", self.config.name, self.state.equity)
        try:
            self.persist_positions()
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s stop position persistence failed: %s", self.config.name, exc)
        self._record_event(
            intent=f"{self.config.symbol.lower()}_stop",
            rationale="lifecycle.stop",
            outcome=Outcome.NOTED,
            equity=self.state.equity,
            pnl=self.state.todays_pnl,
        )
        # v0.1.50: drop any untracked entries so the next session
        # starts clean. See MnqBot.stop for context.
        self._active_entries.clear()

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
        # Wave-6 sage plumbing (2026-04-27): rolling sage-bar buffer.
        self.observe_bar_for_sage(bar)
        if not self.check_risk():
            return
        regime = self._infer_regime(bar)
        # AI-Optimized strategy stack takes priority when wired.
        if self._strategy_adapter is not None:
            self._strategy_adapter.kill_switch_active = self.state.is_killed
            router_signal = self._strategy_adapter.push_bar(bar)
            if router_signal is not None:
                router_signal = self._prepare_signal_for_routing(
                    router_signal,
                    bar,
                    regime,
                )
                atr = bar.get("atr_14", bar["close"] * 0.02)
                lev = self.effective_leverage(
                    router_signal.confidence,
                    bar["close"],
                    atr,
                )
                if lev is not None:
                    router_signal.meta["leverage"] = round(lev, 1)
                    # v0.1.50: stash entry BEFORE routing so a
                    # synchronous fill finds the record.
                    self._track_entry_from_signal(router_signal, regime)
                    await self.on_signal(router_signal)
                self._tick_retrospective(regime)
                return
        self._tick_retrospective(regime)
        for setup_fn in (self.trend_follow, self.mean_revert, self.breakout):
            signal = setup_fn(bar)
            if signal is not None:
                signal = self._prepare_signal_for_routing(signal, bar, regime)
                atr = bar.get("atr_14", bar["close"] * 0.02)
                lev = self.effective_leverage(signal.confidence, bar["close"], atr)
                if lev is not None:
                    signal.meta["leverage"] = round(lev, 1)
                    self._track_entry_from_signal(signal, regime)
                    await self.on_signal(signal)
                break

    async def on_signal(self, signal: Signal) -> OrderResult | None:
        """Route a signal through JARVIS and, if allowed, to the venue.

        Flow:
          1. Log.
          2. Gate through JARVIS on LONG/SHORT entries only. Closes are
             exit-only and always proceed.
          3. Size via risk%; apply CONDITIONAL cap to both qty and
             leverage (so a REDUCE tier cuts the dollar risk, not just
             the coin count).
          4. Route. Broker exceptions become ``None`` returns.
        """
        lev = signal.meta.get("leverage", "?")
        logger.info(
            "%s signal: %s @ %.4f conf=%.1f lev=%sx",
            self.config.name,
            signal.type.value,
            signal.price,
            signal.confidence,
            lev,
        )

        _is_entry = signal.type in (SignalType.LONG, SignalType.SHORT)
        cap: float | None = None
        if _is_entry:
            sage_bars = self.recent_sage_bars()
            allowed, cap, code = self._ask_jarvis(
                ActionType.ORDER_PLACE,
                rationale=f"{signal.type.value} {self.config.symbol}",
                side=signal.type.value,
                symbol=signal.symbol,
                price=signal.price,
                confidence=signal.confidence,
                leverage=float(lev) if isinstance(lev, int | float) else 1.0,
                sage_bars=sage_bars,
                entry_price=signal.price,
                instrument_class="crypto",
            )
            if not allowed:
                self._record_event(
                    intent=f"{self.config.symbol.lower()}_order_blocked",
                    rationale=f"jarvis refused ORDER_PLACE: {code}",
                    outcome=Outcome.BLOCKED,
                    signal=signal.type.value,
                    price=signal.price,
                )
                return None
            # Apply CONDITIONAL cap to the leverage BEFORE sizing so
            # the qty calculation uses the capped leverage. This way
            # a 0.5 cap halves both size and leverage -- dollar risk
            # drops 2x, not 4x.
            if cap is not None and cap < 1.0:
                effective_lev = (
                    float(
                        signal.meta.get("leverage", 1.0),
                    )
                    * cap
                )
                signal.meta["leverage"] = round(max(effective_lev, 1.0), 2)

        if self._router is None:
            self._record_event(
                intent=f"{self.config.symbol.lower()}_paper_sim",
                rationale="no router -- log-only mode",
                outcome=Outcome.NOTED,
                signal=signal.type.value,
                price=signal.price,
            )
            return None

        qty = self._size_from_signal(signal)
        if qty <= 0.0:
            logger.debug("%s signal skipped: qty=%.8f <= 0", self.config.name, qty)
            self._record_event(
                intent=f"{self.config.symbol.lower()}_order_zero_qty",
                rationale="risk sizing returned zero",
                outcome=Outcome.NOTED,
                signal=signal.type.value,
                cap=cap,
            )
            return None
        side, reduce_only = self._signal_to_order_side(signal.type)
        req, urgency = self._build_order_request(signal, side, qty, reduce_only)
        try:
            result = await self._router.place_with_failover(req, urgency=urgency)
        except Exception as e:  # noqa: BLE001 - router logs & alerts internally
            logger.error("%s route failed: %s", self.config.name, e)
            self._record_event(
                intent=f"{self.config.symbol.lower()}_order_route_error",
                rationale=str(e),
                outcome=Outcome.FAILED,
                signal=signal.type.value,
                qty=qty,
            )
            return None
        if result.status is OrderStatus.REJECTED:
            logger.warning("%s order rejected: id=%s", self.config.name, result.order_id)
            self._record_event(
                intent=f"{self.config.symbol.lower()}_order_rejected",
                rationale="venue rejected order",
                outcome=Outcome.FAILED,
                order_id=result.order_id,
                signal=signal.type.value,
                qty=qty,
            )
        else:
            self._record_event(
                intent=f"{self.config.symbol.lower()}_order_routed",
                rationale="order accepted by venue",
                outcome=Outcome.EXECUTED,
                order_id=result.order_id,
                signal=signal.type.value,
                qty=qty,
                leverage=signal.meta.get("leverage"),
                cap=cap,
            )
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
        # 2026-04-27 devils-advocate: half-size for first 30 days via
        # effective_risk_per_trade_pct (auto-reverts to 1.0 multiplier
        # post-window). See strategies.warmup_policy.
        risk_usd = self.state.equity * (self.effective_risk_per_trade_pct() / 100.0)
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

    def _build_order_request(
        self,
        signal: Signal,
        side: Side,
        qty: float,
        reduce_only: bool,
    ) -> tuple[OrderRequest, str]:
        """Build the OrderRequest + urgency for a routed signal.

        Default is a MARKET order at normal urgency (ETH/SOL on a
        deep book). Subclasses that trade thin books (XRP) override
        this to prefer POST_ONLY at signal.price with low urgency.
        """
        req = OrderRequest(
            symbol=self._venue_symbol,
            side=side,
            qty=qty,
            reduce_only=reduce_only,
        )
        return req, "normal"

    # ── Decision Logic ──

    def evaluate_entry(self, bar: dict[str, Any], confluence_score: float) -> bool:
        atr = bar.get("atr_14", bar.get("close", 1) * 0.02)
        lev = self.effective_leverage(confluence_score, bar.get("close", 0), atr)
        return lev is not None and self.check_risk()

    def evaluate_exit(self, position: Position) -> bool:
        # 2026-04-27: warm-up-aware exit threshold; matches the entry sizing.
        risk_usd = self.effective_risk_per_trade_pct() / 100 * self.state.equity
        if position.unrealized_pnl <= -risk_usd:
            return True
        return position.unrealized_pnl >= 3.0 * risk_usd

    # ── Regime Filter (v0.1.49) ──

    @staticmethod
    def _infer_regime(bar: dict[str, Any]) -> RegimeType:
        """Classify regime using ADX (ETH-calibrated thresholds).

        ETH perp markets are noisier than futures indices; ADX
        cutoffs are pulled in a notch from the MnqBot defaults
        (30/20) to 25/18 so trending windows are recognized at the
        lower ADX values typical of crypto.
        """
        adx: float = bar.get("adx_14", 18.0)
        if adx >= _ADX_TREND_ETH:
            return RegimeType.TRENDING
        if adx >= _ADX_TRANSITION_ETH:
            return RegimeType.TRANSITION
        return RegimeType.RANGING

    # ── Retrospective entry tracking (v0.1.50) ──

    def _track_entry_from_signal(
        self,
        signal: Signal,
        regime: RegimeType,
    ) -> None:
        """Stash the entry context for later pnl_r computation.

        See :meth:`MnqBot._track_entry_from_signal` for semantics.
        The ETH variant shares the same adapter helpers; only the
        regime classifier differs.
        """
        from eta_engine.bots.retrospective_adapter import (
            ActiveEntry,
            compute_risk_usd,
            default_strategy_for_symbol,
            is_entry_signal_type,
            map_regime,
        )

        if not is_entry_signal_type(signal.type):
            return
        risk_usd = compute_risk_usd(
            equity=self.state.equity,
            # 2026-04-27: pass effective (warm-up-adjusted) risk pct.
            risk_per_trade_pct=self.effective_risk_per_trade_pct(),
        )
        if risk_usd <= 0.0:
            return
        strat = self._default_retrospective_strategy or default_strategy_for_symbol(self.config.symbol)
        from datetime import UTC, datetime

        self._active_entries[signal.symbol] = ActiveEntry(
            symbol=signal.symbol,
            risk_usd=risk_usd,
            strategy=strat,
            regime=map_regime(regime),
            opened_at_utc=datetime.now(UTC),
        )

    @property
    def active_entries(self) -> dict[str, ActiveEntry]:
        """Return a snapshot of currently-tracked entries."""
        return dict(self._active_entries)

    def record_fill(self, fill: Fill) -> RetrospectiveReport | None:
        """Process one fill. On close, auto-invoke the retrospective.

        See :meth:`MnqBot.record_fill` for the semantics. The ETH
        variant shares the same one-call integration contract.
        """
        self.update_state(fill)
        from eta_engine.bots.retrospective_adapter import is_close_fill

        if not is_close_fill(fill):
            return None
        active = self._active_entries.pop(fill.symbol, None)
        if active is not None:
            risk_usd = active.risk_usd
            strategy: StrategyId | None = active.strategy
            regime: RegimeLabel | None = active.regime
        else:
            risk_usd = fill.risk_at_entry
            strategy = None
            regime = None
        if risk_usd <= 0.0:
            logger.debug(
                "%s record_fill: no risk-at-entry for %s; skipping retrospective auto-invoke",
                self.config.name,
                fill.symbol,
            )
            return None
        pnl_r = fill.realized_pnl / risk_usd
        return self.record_trade_outcome(
            pnl_r=pnl_r,
            strategy=strategy,
            regime=regime,
        )

    # ── Retrospective wiring (v0.1.49) ──

    @property
    def retrospective_manager(self) -> RetrospectiveManager | None:
        """Return the wired :class:`RetrospectiveManager`, if any."""
        return self._retrospective_manager

    def _tick_retrospective(self, regime: RegimeType) -> None:
        """Push regime + equity into the retrospective manager.

        Safe no-op when no manager is wired. Exceptions raised by
        the manager are logged and swallowed -- the trading loop
        must never crash from a retrospective.
        """
        if self._retrospective_manager is None:
            return
        from eta_engine.bots.retrospective_adapter import map_regime

        try:
            self._retrospective_manager.on_bar(
                regime=map_regime(regime),
                equity=self.state.equity,
            )
        except Exception as e:  # noqa: BLE001 - never crash the loop
            logger.warning(
                "%s retrospective on_bar failed: %s",
                self.config.name,
                e,
            )

    def record_trade_outcome(
        self,
        *,
        pnl_r: float,
        strategy: StrategyId | None = None,
        regime: RegimeLabel | None = None,
    ) -> RetrospectiveReport | None:
        """Feed a closed trade into the retrospective manager.

        See :meth:`MnqBot.record_trade_outcome` for semantics. The
        ETH variant shares the interface and fallback logic.
        """
        if self._retrospective_manager is None:
            return None
        from eta_engine.bots.retrospective_adapter import (
            build_trade_outcome,
            default_strategy_for_symbol,
        )
        from eta_engine.strategies.adaptive_sizing import RegimeLabel

        strat = strategy or self._default_retrospective_strategy or default_strategy_for_symbol(self.config.symbol)
        reg = regime or RegimeLabel.TRANSITION
        outcome = build_trade_outcome(
            strategy=strat,
            regime=reg,
            pnl_r=pnl_r,
            equity_after=self.state.equity,
        )
        try:
            return self._retrospective_manager.record_trade(outcome)
        except Exception as e:  # noqa: BLE001 - never crash the loop
            logger.warning(
                "%s retrospective record_trade failed: %s",
                self.config.name,
                e,
            )
            return None
