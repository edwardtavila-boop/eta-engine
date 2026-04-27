"""MNQ Futures Bot -- ENGINE tier, 4 setups from APEX v3.

Micro E-mini Nasdaq-100. Tick $0.25, tick value $0.50, point value $2.00. TF: 5m/1m/1s.

Injectable dependencies
-----------------------
* `router`: an ``eta_engine.venues.router.SmartRouter`` instance (or anything
  exposing ``async place_with_failover(OrderRequest) -> OrderResult``). If
  omitted, ``on_signal`` falls back to log-only -- preserves the zero-venue
  test/dry-run behavior the fleet relied on before wiring.
* `jarvis`: optional :class:`JarvisAdmin`. When supplied, every
  risk-adding action (STRATEGY_DEPLOY on start, ORDER_PLACE on every
  outgoing signal) is gated through
  :meth:`JarvisAdmin.request_approval` BEFORE the venue sees the
  order. DENIED / DEFERRED verdicts refuse the action; CONDITIONAL
  verdicts carry a ``size_cap_mult`` that is applied to the contract
  count. When ``None`` the bot runs without JARVIS oversight (the
  pre-v0.1.57 path, still used by unit tests).
* `journal`: optional :class:`DecisionJournal` for auditing every
  entry / refusal / fill.
* `provide_ctx`: optional zero-arg callable returning a fresh
  :class:`JarvisContext`. When ``None`` and ``jarvis`` is set, the
  caller MUST have wired an engine into the ``JarvisAdmin`` so it
  can self-tick.
* `session_levels`: pre-computed PDH/PDL/ONH/ONL/VWAP anchors. Feed them
  once per day; ``sweep_check`` scans them.
* `tradovate_symbol`: Tradovate contract symbol (e.g. ``MNQH6``). Defaults to
  ``MNQ`` so the spec stays broker-agnostic.
* `strategy_adapter`: optional
  :class:`eta_engine.strategies.engine_adapter.RouterAdapter`. When
  wired, ``on_bar`` first asks the adapter (the six AI-optimized SMC/ICT
  strategies) for a signal; on a miss it falls through to the legacy
  4-setup loop. Leave ``None`` for the pre-v0.1.34 path.
* `auto_wire_ai_strategies`: when ``True`` and ``strategy_adapter`` is
  ``None``, ``start()`` builds a fully-wired :class:`RouterAdapter`
  (with :class:`RuntimeAllowlistCache` + :class:`AllowlistScheduler`)
  via :func:`eta_engine.strategies.live_adapter.build_live_adapter`
  so the live bot auto-loops bar ingest -> OOS qualifier -> allowlist
  refresh -> dispatch with zero operator involvement. v0.1.45+.
* `ai_strategy_config`: kwargs forwarded to ``build_live_adapter`` when
  ``auto_wire_ai_strategies`` is active. Lets operators override TTL /
  trigger cadence / warmup / decision_sink without subclassing the bot.

Trailing stop
-------------
``evaluate_exit`` tracks the peak unrealized PnL per position id since entry
and exits when PnL retraces ``trailing_drawdown_r`` R off the peak (default 1R).
A hard stop at ``-risk_per_trade_pct`` R and a hard 2R target remain in place.

JARVIS takeover
---------------
Supplying ``jarvis=JarvisAdmin(...)`` puts the MNQ bot in "takeover
mode" -- JARVIS gates every order. In this mode:

* ``start()`` requests :attr:`ActionType.STRATEGY_DEPLOY`; a refusal
  pauses the bot (``state.is_paused = True``) and logs the reason code.
* ``on_signal()`` requests :attr:`ActionType.ORDER_PLACE` before each
  route. Refused -> no order. CONDITIONAL -> contracts scaled by
  ``size_cap_mult``.
* ``record_event()`` writes one journal line per decision for the
  operator dashboard.
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
    SweepResult,
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
    from eta_engine.core.kill_switch_latch import KillSwitchLatch
    from eta_engine.core.session_gate import SessionGate
    from eta_engine.strategies.adaptive_sizing import RegimeLabel
    from eta_engine.strategies.engine_adapter import RouterAdapter
    from eta_engine.strategies.models import StrategyId
    from eta_engine.strategies.retrospective import RetrospectiveReport
    from eta_engine.strategies.retrospective_wiring import (
        RetrospectiveManager,
    )

logger = logging.getLogger(__name__)

MNQ_CONFIG = BotConfig(
    name="MNQ-Engine",
    symbol="MNQ",
    tier=Tier.FUTURES,
    baseline_usd=5500.0,
    starting_capital_usd=5000.0,
    max_leverage=5.0,
    risk_per_trade_pct=1.0,
    daily_loss_cap_pct=2.5,
    max_dd_kill_pct=8.0,
    margin_mode=MarginMode.CROSS,
)
TICK_SIZE: float = 0.25
TICK_VALUE: float = 0.50
POINT_VALUE: float = 2.00

# Default trailing-stop: exit after 1R pullback from peak unrealized.
_DEFAULT_TRAILING_R: float = 1.0
# Default ORB volume-confirmation multiplier.
_ORB_VOLUME_MULT: float = 1.3
# Default EMA-touch distance as fraction of EMA (10 bps).
_EMA_TOUCH_FRAC: float = 0.001
# Default mean-reversion z-score.
_MR_Z_ENTRY: float = 2.0
# Default ADX thresholds.
_ADX_TREND: float = 30.0
_ADX_TRANSITION: float = 20.0


class _Router(Protocol):
    async def place_with_failover(self, req: OrderRequest) -> OrderResult: ...


class MnqBot(BaseBot):
    """MNQ futures bot -- 4 setups, regime-filtered, sweep-aware, router-backed.

    JARVIS-ready: pass ``jarvis=JarvisAdmin(...)`` to the constructor and
    every ORDER_PLACE / STRATEGY_DEPLOY goes through JARVIS before
    hitting the venue.
    """

    # Instrument dollars-per-point. Subclasses (NqBot) override with $20.
    POINT_VALUE_USD: float = POINT_VALUE

    # Subsystem identity -- stable across the audit log.
    SUBSYSTEM: SubsystemId = SubsystemId.BOT_MNQ

    def __init__(
        self,
        config: BotConfig | None = None,
        *,
        router: _Router | None = None,
        jarvis: JarvisAdmin | None = None,
        journal: DecisionJournal | None = None,
        provide_ctx: Callable[[], JarvisContext] | None = None,
        session_levels: list[float] | None = None,
        tradovate_symbol: str | None = None,
        trailing_drawdown_r: float = _DEFAULT_TRAILING_R,
        strategy_adapter: RouterAdapter | None = None,
        auto_wire_ai_strategies: bool = False,
        ai_strategy_config: dict[str, Any] | None = None,
        retrospective_manager: RetrospectiveManager | None = None,
        auto_wire_retrospective: bool = False,
        retrospective_config: dict[str, Any] | None = None,
        default_retrospective_strategy: StrategyId | None = None,
        session_gate: SessionGate | None = None,
        kill_switch_latch: KillSwitchLatch | None = None,
    ) -> None:
        super().__init__(config or MNQ_CONFIG)
        self._liquidity_levels: list[float] = list(session_levels or [])
        self._router = router
        self._jarvis = jarvis
        self._journal = journal
        self._provide_ctx = provide_ctx
        self._tradovate_symbol = tradovate_symbol or self.config.symbol
        self._trailing_drawdown_r = trailing_drawdown_r
        self._strategy_adapter = strategy_adapter
        self._auto_wire_ai_strategies = auto_wire_ai_strategies
        self._ai_strategy_config: dict[str, Any] = dict(ai_strategy_config) if ai_strategy_config else {}
        self._retrospective_manager = retrospective_manager
        self._auto_wire_retrospective = auto_wire_retrospective
        self._retrospective_config: dict[str, Any] = dict(retrospective_config) if retrospective_config else {}
        self._default_retrospective_strategy = default_retrospective_strategy
        # D1/D4/D5 -- session gate + persistent kill-switch latch.
        # Both are opt-in (legacy callers pass None; the bot stays
        # backward-compatible). When present:
        #   * session_gate replaces the hardcoded
        #     session_allows_entries=True via the RouterAdapter wiring,
        #     fusing RTH / news-blackout / EoD cutoff into one decision.
        #   * kill_switch_latch.boot_allowed() gates ``start()`` so a
        #     previously-TRIPPED latch refuses to re-arm the bot.
        self._session_gate = session_gate
        self._kill_switch_latch = kill_switch_latch
        # One-shot guard so we only emit EoD close signals once per
        # cutoff window (otherwise every bar past 15:59 CT would
        # re-emit closes). Reset on stop().
        self._eod_flatten_fired: bool = False
        # position_id -> peak unrealized PnL since entry
        self._trailing_peak: dict[str, float] = {}
        # v0.1.50: symbol -> ActiveEntry. Populated when an entry
        # signal fires in on_bar; consumed in record_fill when the
        # matching close fill arrives. Per-symbol key assumes at
        # most one open position per symbol (the fleet-wide
        # invariant). A second entry on the same symbol before a
        # close overwrites the first -- acceptable for the pilot.
        self._active_entries: dict[str, ActiveEntry] = {}

    # ── JARVIS gating helpers ──

    def _ask_jarvis(
        self,
        action: ActionType,
        **payload: Any,  # noqa: ANN401 -- payload is intentionally untyped
    ) -> tuple[bool, float | None, str]:
        """Gate a risk-adding action through JARVIS.

        When ``self._jarvis`` is ``None`` (legacy / test mode) this
        returns ``(True, None, "no_jarvis")`` so callers behave
        exactly as they did pre-v0.1.57. When JARVIS is wired the
        call returns the real verdict.
        """
        if self._jarvis is None:
            return True, None, "no_jarvis"
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
        """Ask JARVIS which model tier to use for a given task.

        Bot-side convenience for operators running ad-hoc retros /
        explanations via JARVIS. Returns :attr:`ModelTier.SONNET`
        when no JARVIS is wired (safe default).
        """
        if self._jarvis is None:
            from eta_engine.brain.model_policy import ModelTier as _ModelTier

            return _ModelTier.SONNET
        return pick_llm_tier(
            self._jarvis,
            subsystem=self.SUBSYSTEM,
            category=category,
            rationale=rationale,
        )

    # ── Lifecycle ──

    async def start(self) -> None:
        # D5: persistent kill-switch latch check. If a prior session
        # tripped a catastrophic kill (FLATTEN_ALL, tier-A preemptive,
        # tier-B), the latch is TRIPPED on disk and the bot refuses to
        # re-arm until the operator runs:
        #   python -m eta_engine.scripts.clear_kill_switch \
        #       --confirm --operator <name>
        # This is the single defense against a crash-loop silently
        # re-arming after an eval-saving kill.
        if self._kill_switch_latch is not None:
            boot_ok, boot_reason = self._kill_switch_latch.boot_allowed()
            if not boot_ok:
                logger.critical(
                    "MNQ bot refused to start (kill-switch latch): %s",
                    boot_reason,
                )
                self._record_event(
                    intent="mnq_start_blocked",
                    rationale=f"kill_switch_latch: {boot_reason}",
                    outcome=Outcome.BLOCKED,
                )
                self.state.is_paused = True
                return

        # Ask JARVIS for STRATEGY_DEPLOY permission BEFORE wiring
        # any sub-engines so a kill / stand-aside context denies the
        # strategy before a single order is placed. No-op when
        # jarvis is None (legacy path).
        allowed, _cap, code = self._ask_jarvis(
            ActionType.STRATEGY_DEPLOY,
            rationale="arming MNQ futures bot",
            mode="engine",
        )
        if not allowed:
            logger.warning("MNQ bot refused to start: %s", code)
            self._record_event(
                intent="mnq_start_blocked",
                rationale=f"jarvis refused STRATEGY_DEPLOY: {code}",
                outcome=Outcome.BLOCKED,
            )
            self.state.is_paused = True
            return

        # Auto-wire the OOS-governed AI-Optimized strategy stack if
        # requested and none has been supplied by the operator. Local
        # import so the bot stays importable in environments that
        # don't load the strategies subpackage (e.g. unit tests for
        # the legacy 4-setup path).
        if self._auto_wire_ai_strategies and self._strategy_adapter is None:
            from eta_engine.strategies.live_adapter import (
                build_live_adapter,
            )

            self._strategy_adapter = build_live_adapter(
                self.config.symbol,
                **self._ai_strategy_config,
            )
            logger.info(
                "MNQ bot auto-wired AI-Optimized strategy adapter (asset=%s, scheduler=on)",
                self.config.symbol,
            )
        # D1/D4: wire the SessionGate into the RouterAdapter so
        # ``session_allows_entries`` is driven by the fused RTH / news /
        # EoD verdict instead of the hardcoded True. The adapter's
        # own master-flag semantics still apply: if the operator has
        # ``session_allows_entries=False`` on the adapter, the gate is
        # ignored (master kill wins).
        if self._strategy_adapter is not None and self._session_gate is not None:
            self._strategy_adapter.session_gate = self._session_gate
            logger.info(
                "MNQ bot attached SessionGate to strategy adapter (tz=%s, rth=%s..%s, eod=%s)",
                self._session_gate.config.timezone_name,
                self._session_gate.config.rth_start_local.isoformat(),
                self._session_gate.config.rth_end_local.isoformat(),
                self._session_gate.config.eod_cutoff_local.isoformat(),
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
                "MNQ bot auto-wired RetrospectiveManager (starting_equity=$%.2f)",
                self.config.starting_capital_usd,
            )
        logger.info(
            "MNQ bot starting | capital=$%.2f symbol=%s levels=%d router=%s jarvis=%s retrospective=%s",
            self.config.starting_capital_usd,
            self._tradovate_symbol,
            len(self._liquidity_levels),
            "yes" if self._router is not None else "no",
            "yes" if self._jarvis is not None else "no",
            "yes" if self._retrospective_manager is not None else "no",
        )
        self._record_event(
            intent="mnq_start",
            rationale="jarvis approved STRATEGY_DEPLOY" if self._jarvis else "no_jarvis",
            outcome=Outcome.EXECUTED,
            symbol=self._tradovate_symbol,
            router="yes" if self._router is not None else "no",
            jarvis="yes" if self._jarvis is not None else "no",
        )

    async def _maybe_flatten_eod(self, bar: dict[str, Any]) -> None:
        """D1: emit close signals past the EoD cutoff.

        Delegates to :meth:`RouterAdapter.should_flatten_eod` so the
        fused gate logic stays in one place. Fires at most once per
        cutoff window -- the ``_eod_flatten_fired`` latch is reset when
        the gate reports ``no_eod_action`` again (i.e. the next RTH
        session has opened).

        When no open positions exist this is a no-op but still logs
        the intent so the audit journal records that the gate fired.
        """
        if self._strategy_adapter is None:
            return
        should_flatten, reason = self._strategy_adapter.should_flatten_eod(bar)
        if not should_flatten:
            if self._eod_flatten_fired and reason == "no_eod_action":
                # Gate has re-opened -- reset the latch for tomorrow.
                self._eod_flatten_fired = False
            return
        if self._eod_flatten_fired:
            # Already flattened on a previous bar in this cutoff
            # window; do not re-emit.
            return
        self._eod_flatten_fired = True
        open_count = len(self.state.open_positions)
        logger.warning(
            "MNQ EoD flatten fired (reason=%s, open_positions=%d)",
            reason,
            open_count,
        )
        self._record_event(
            intent="mnq_eod_flatten",
            rationale=f"session_gate: {reason}",
            outcome=Outcome.EXECUTED,
            open_positions=open_count,
        )
        # Emit a close signal for each position the bot knows about.
        # The broker layer is the ultimate source of truth for open
        # positions; emitting through ``on_signal`` keeps the audit
        # trail consistent even if ``state.open_positions`` is a
        # partial view.
        last_close = float(bar.get("close", 0.0))
        for pos in list(self.state.open_positions):
            close_type = SignalType.CLOSE_LONG if pos.side.lower() in {"long", "buy"} else SignalType.CLOSE_SHORT
            await self.on_signal(
                Signal(
                    type=close_type,
                    symbol=pos.symbol,
                    price=last_close,
                    size=pos.size,
                    confidence=10.0,  # forced close; confidence irrelevant
                    meta={
                        "setup": "eod_flatten",
                        "gate_reason": reason,
                        "source": "session_gate",
                    },
                )
            )

    async def stop(self) -> None:
        logger.info("MNQ bot stopping | equity=$%.2f pnl=$%.2f", self.state.equity, self.state.todays_pnl)
        self._record_event(
            intent="mnq_stop",
            rationale="lifecycle.stop",
            outcome=Outcome.NOTED,
            equity=self.state.equity,
            pnl=self.state.todays_pnl,
        )
        # Reset the D1 EoD latch so the next session can fire it again.
        self._eod_flatten_fired = False
        # Clear trailing state so a restarted bot doesn't carry stale peaks.
        self._trailing_peak.clear()
        # v0.1.50: drop any untracked entries so the next session
        # starts clean. On reconnect the fill pipeline should see
        # the live open positions from reconciliation and re-populate
        # risk_at_entry via Fill.risk_at_entry on the next close.
        self._active_entries.clear()

    def load_session_levels(self, levels: list[float]) -> None:
        """Replace current-session liquidity anchors (PDH/PDL/ONH/ONL/VWAP)."""
        self._liquidity_levels = list(levels)
        logger.debug("MNQ levels reloaded: %d anchors", len(self._liquidity_levels))

    # ── Market Events ──

    async def on_bar(self, bar: dict[str, Any]) -> None:
        # Wave-6 sage plumbing (2026-04-27): keep a rolling 200-bar buffer
        # so JARVIS v22 can consult the multi-school sage on every order.
        # No-op cost when V22_SAGE_MODULATION=false (just one deque append).
        self.observe_bar_for_sage(bar)
        if not self.check_risk():
            return
        # D1: EoD flatten check. When the gate tells us we've crossed
        # the EoD cutoff (default 15:59 CT), emit close signals for
        # every open position and refuse new entries until the next
        # RTH session. The gate itself already sets
        # ``session_allows_entries=False`` past the cutoff so the
        # router returns FLAT -- this block makes the intent explicit
        # for observability, the audit journal, and any broker-side
        # automation keyed off signal stream.
        if self._strategy_adapter is not None and self._session_gate is not None:
            await self._maybe_flatten_eod(bar)
        # AI-Optimized strategy stack takes priority when wired.
        if self._strategy_adapter is not None:
            # Propagate bot-state gates into the adapter context.
            self._strategy_adapter.kill_switch_active = self.state.is_killed
            router_signal = self._strategy_adapter.push_bar(bar)
            if router_signal is not None:
                regime = self.regime_filter(bar)
                # v0.1.50: stash entry context BEFORE routing so a
                # synchronous fill (simulator / unit tests) has the
                # record available immediately on record_fill.
                self._track_entry_from_signal(router_signal, regime)
                await self.on_signal(router_signal)
                # Tick the retrospective loop even on adapter signals
                # so regime transitions are seen.
                self._tick_retrospective(regime)
                return
        regime = self.regime_filter(bar)
        self._tick_retrospective(regime)
        sweep = self.sweep_check(bar, self._liquidity_levels)
        for setup_fn in (self.orb_breakout, self.ema_pullback, self.sweep_reclaim, self.mean_reversion):
            signal = setup_fn(bar, regime, sweep)
            if signal is not None:
                self._track_entry_from_signal(signal, regime)
                await self.on_signal(signal)
                break  # one signal per bar

    async def on_signal(self, signal: Signal) -> OrderResult | None:
        """Route a signal to the venue through JARVIS.

        Flow:

        1. Log the signal.
        2. Gate through JARVIS (``ORDER_PLACE``). When ``jarvis`` is
           ``None`` this is a no-op approval.
           * DENIED / DEFERRED -> return ``None`` (no router call).
           * CONDITIONAL -> proceed with ``size_cap_mult`` applied to qty.
        3. Size the signal; skip if the final qty is <= 0.
        4. Route. Broker exceptions become ``None`` returns.

        Returns the broker-side ``OrderResult`` when a router is wired, or
        ``None`` in log-only mode. Entry signals convert to market orders
        sized from ``_size_from_signal``; exit signals use ``reduce_only``.
        """
        logger.info("MNQ signal: %s @ %.2f conf=%.1f", signal.type.value, signal.price, signal.confidence)

        # JARVIS gate -- refuses orders during kill / stand-aside.
        # Exit signals ALWAYS proceed (they reduce risk); JARVIS
        # itself treats POSITION_FLATTEN / ORDER_CANCEL as exit-only,
        # and closes here use reduce_only so they're semantically
        # the same thing. We still ask JARVIS on entries only.
        _is_entry = signal.type in (SignalType.LONG, SignalType.SHORT)
        cap: float | None = None
        if _is_entry:
            # Wave-6 sage plumbing: hand JARVIS the rolling bar history
            # so v22_sage_confluence can run the 23-school consultation
            # when V22_SAGE_MODULATION=true. Empty list when the buffer
            # hasn't filled yet -- v22 falls back to v17 silently.
            sage_bars = self.recent_sage_bars()
            allowed, cap, code = self._ask_jarvis(
                ActionType.ORDER_PLACE,
                rationale=f"{signal.type.value} {signal.meta.get('setup', '?')}",
                side=signal.type.value,
                symbol=signal.symbol,
                price=signal.price,
                confidence=signal.confidence,
                sage_bars=sage_bars,
                entry_price=signal.price,
            )
            if not allowed:
                self._record_event(
                    intent="mnq_order_blocked",
                    rationale=f"jarvis refused ORDER_PLACE: {code}",
                    outcome=Outcome.BLOCKED,
                    signal=signal.type.value,
                    price=signal.price,
                )
                return None

        if self._router is None:
            self._record_event(
                intent="mnq_paper_sim",
                rationale="no router -- log-only mode",
                outcome=Outcome.NOTED,
                signal=signal.type.value,
                price=signal.price,
            )
            return None

        base_qty = self._size_from_signal(signal)
        # Apply JARVIS CONDITIONAL size cap (entry signals only; exits
        # stay unscaled so a reduce_only close always fully offsets).
        qty = base_qty
        if _is_entry and cap is not None and cap < 1.0:
            qty = float(int(base_qty * cap))
        if qty <= 0.0:
            logger.debug(
                "MNQ signal skipped: qty=%.4f <= 0 (base=%.4f cap=%s)",
                qty,
                base_qty,
                f"{cap:.3f}" if cap is not None else "none",
            )
            self._record_event(
                intent="mnq_order_zero_qty",
                rationale="risk sizing returned zero",
                outcome=Outcome.NOTED,
                signal=signal.type.value,
                base_qty=base_qty,
                cap=cap,
            )
            return None

        side, reduce_only = self._signal_to_order_side(signal.type)
        req = OrderRequest(
            symbol=self._tradovate_symbol,
            side=side,
            qty=qty,
            reduce_only=reduce_only,
        )
        try:
            result = await self._router.place_with_failover(req)
        except Exception as e:  # noqa: BLE001 - upstream logs + alert, we just return None
            logger.error("MNQ route failed: %s", e)
            self._record_event(
                intent="mnq_order_route_error",
                rationale=str(e),
                outcome=Outcome.FAILED,
                signal=signal.type.value,
                qty=qty,
            )
            return None
        if result.status is OrderStatus.REJECTED:
            logger.warning("MNQ order rejected: id=%s", result.order_id)
            self._record_event(
                intent="mnq_order_rejected",
                rationale="venue rejected order",
                outcome=Outcome.FAILED,
                order_id=result.order_id,
                signal=signal.type.value,
                qty=qty,
            )
        else:
            self._record_event(
                intent="mnq_order_routed",
                rationale="order accepted by venue",
                outcome=Outcome.EXECUTED,
                order_id=result.order_id,
                signal=signal.type.value,
                qty=qty,
                cap=cap,
            )
        return result

    # ── Sizing ──

    def _size_from_signal(self, signal: Signal) -> float:
        """Return contract count so that stop-out risk <= risk_per_trade_pct.

        Uses a symmetric 1R stop distance derived from ``meta['stop_distance']``
        (points) if supplied; otherwise falls back to 0.5% of entry price as a
        conservative default stop.
        """
        if signal.size > 0.0:
            return float(signal.size)
        risk_usd = self.state.equity * (self.config.risk_per_trade_pct / 100.0)
        stop_distance_pts: float = float(signal.meta.get("stop_distance", signal.price * 0.005))
        if stop_distance_pts <= 0.0:
            return 0.0
        risk_per_contract = stop_distance_pts * self.POINT_VALUE_USD
        if risk_per_contract <= 0.0:
            return 0.0
        contracts = risk_usd / risk_per_contract
        # Whole contracts, hard floor at 0. Caller decides to skip if 0.
        return float(int(contracts))

    @staticmethod
    def _signal_to_order_side(sig_type: SignalType) -> tuple[Side, bool]:
        """Map a SignalType onto (venue Side, reduce_only)."""
        if sig_type is SignalType.LONG:
            return Side.BUY, False
        if sig_type is SignalType.SHORT:
            return Side.SELL, False
        if sig_type is SignalType.CLOSE_LONG:
            return Side.SELL, True
        if sig_type is SignalType.CLOSE_SHORT:
            return Side.BUY, True
        # GRID_* signals are not used on the futures bot; default to BUY flat.
        return Side.BUY, False

    # ── Decision Logic ──

    def evaluate_entry(self, bar: dict[str, Any], confluence_score: float) -> bool:
        if confluence_score < 5.0:
            return False
        regime = self.regime_filter(bar)
        if regime == RegimeType.RANGING and confluence_score < 7.0:
            return False
        risk_ok = self.check_risk()
        return risk_ok and self.state.trades_today < 6

    def evaluate_exit(self, position: Position) -> bool:
        """Exit logic: hard stop, 2R target, or trailing drawdown off peak.

        Trailing state is per-position (keyed by ``symbol+entry_price``) and
        cleared when the bot stops. The trail engages only after price has
        moved at least 1R in favor — below that, only the hard stop / target
        are active.
        """
        risk_r_usd = self.config.risk_per_trade_pct / 100.0 * self.state.equity
        # 1) hard stop
        if position.unrealized_pnl <= -risk_r_usd:
            self._trailing_peak.pop(self._pos_key(position), None)
            return True
        # 2) fixed 2R target
        r_target = 2.0 * abs(position.entry_price * position.size * 0.01)
        if position.unrealized_pnl >= r_target:
            self._trailing_peak.pop(self._pos_key(position), None)
            return True
        # 3) trailing drawdown off the peak (only engages once >= 1R in profit)
        key = self._pos_key(position)
        peak = max(self._trailing_peak.get(key, 0.0), position.unrealized_pnl)
        self._trailing_peak[key] = peak
        if peak >= risk_r_usd:
            drawdown = peak - position.unrealized_pnl
            if drawdown >= self._trailing_drawdown_r * risk_r_usd:
                self._trailing_peak.pop(key, None)
                return True
        return False

    @staticmethod
    def _pos_key(position: Position) -> str:
        return f"{position.symbol}@{position.entry_price:.4f}"

    # ── Retrospective entry tracking (v0.1.50) ──

    def _track_entry_from_signal(
        self,
        signal: Signal,
        regime: RegimeType,
    ) -> None:
        """Stash the entry context for later pnl_r computation.

        Called from :meth:`on_bar` immediately before :meth:`on_signal`
        fires a LONG / SHORT entry. Records the risk in USD that is
        about to be committed, the regime seen at entry, and the
        :class:`StrategyId` the retrospective will attribute the
        trade to. When the matching close fill arrives,
        :meth:`record_fill` pops this entry and computes
        ``pnl_r = realized_pnl / risk_usd``.

        Silent no-op for close / grid signals and for signals with
        zero computed risk (equity <= 0 or risk_per_trade_pct <= 0).
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
            risk_per_trade_pct=self.config.risk_per_trade_pct,
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
        """Return a snapshot of currently-tracked entries.

        Mostly for tests and dashboards. The returned dict is a
        copy so callers cannot mutate internal state.
        """
        return dict(self._active_entries)

    def record_fill(self, fill: Fill) -> RetrospectiveReport | None:
        """Process one fill. On close, auto-invoke the retrospective.

        This is the one-call integration point for the fill pipeline.
        It:

        1. Calls :meth:`update_state(fill)` so equity + today's PnL
           are bumped exactly as they were before v0.1.50.
        2. If the fill is a close (``realized_pnl`` non-zero):

           * looks up the tracked :class:`ActiveEntry` for the
             symbol (populated by :meth:`_track_entry_from_signal`);
           * falls back to ``fill.risk_at_entry`` when no tracked
             entry exists (operator-populated pipeline);
           * computes ``pnl_r = realized_pnl / risk_usd``;
           * invokes :meth:`record_trade_outcome` with the entry's
             strategy + regime (or defaults on fallback).

        Returns whatever :meth:`record_trade_outcome` returns (the
        fired :class:`RetrospectiveReport` or ``None``). Failures
        are contained; the trading loop never crashes from a fill.
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
            # Fallback: operator-populated Fill.risk_at_entry.
            risk_usd = fill.risk_at_entry
            strategy = None
            regime = None
        if risk_usd <= 0.0:
            logger.debug(
                "MNQ record_fill: no risk-at-entry for %s; skipping retrospective auto-invoke",
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
        """Return the wired :class:`RetrospectiveManager`, if any.

        Operators and dashboards read this to inspect current policy,
        equity band, and fired reports without reaching into private
        state.
        """
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
            logger.warning("MNQ retrospective on_bar failed: %s", e)

    def record_trade_outcome(
        self,
        *,
        pnl_r: float,
        strategy: StrategyId | None = None,
        regime: RegimeLabel | None = None,
    ) -> RetrospectiveReport | None:
        """Feed a closed trade into the retrospective manager.

        Call from the position-close handler with the realized PnL
        expressed in R units (``realized_pnl / risk_at_entry``). The
        manager updates its journal, fires any triggered report, and
        -- under its cooldown guard -- may apply deltas to its
        internal :class:`SizingPolicy`.

        Parameters
        ----------
        pnl_r:
            Realized PnL in R units. Positive = win, negative = loss,
            zero = scratch.
        strategy:
            Which strategy closed. Defaults to the per-symbol
            fallback from
            :data:`bots.retrospective_adapter.DEFAULT_STRATEGY_FOR_BOT`.
        regime:
            Which regime the trade closed in. ``None`` defaults to
            :attr:`RegimeLabel.TRANSITION` so the bucket key stays
            well-defined.

        Returns the fired :class:`RetrospectiveReport` (or ``None``)
        exactly as :meth:`RetrospectiveManager.record_trade` does.
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
            logger.warning("MNQ retrospective record_trade failed: %s", e)
            return None

    # ── Regime Filter ──

    @staticmethod
    def regime_filter(bar: dict[str, Any]) -> RegimeType:
        """Classify regime using ADX from bar metadata."""
        adx: float = bar.get("adx_14", 20.0)
        if adx >= _ADX_TREND:
            return RegimeType.TRENDING
        if adx >= _ADX_TRANSITION:
            return RegimeType.TRANSITION
        return RegimeType.RANGING

    # ── 4 Setups (from APEX v3 framework) ──

    def orb_breakout(
        self,
        bar: dict[str, Any],
        regime: RegimeType,
        sweep: SweepResult | None,  # noqa: ARG002 - regime/sweep reserved for future filters
    ) -> Signal | None:
        """Opening Range Breakout — first-30m high/low break with volume confirmation."""
        orb_high: float = bar.get("orb_high", 0.0)
        orb_low: float = bar.get("orb_low", 0.0)
        if orb_high == 0.0:
            return None
        vol_ok = bar.get("volume", 0) > bar.get("avg_volume", 1) * _ORB_VOLUME_MULT
        stop_dist = abs(bar.get("atr_14", 5.0)) * 1.5
        if bar["close"] > orb_high and vol_ok:
            return Signal(
                type=SignalType.LONG,
                symbol=self.config.symbol,
                price=bar["close"],
                confidence=7.0,
                meta={"setup": "orb_breakout", "stop_distance": stop_dist},
            )
        if bar["close"] < orb_low and vol_ok:
            return Signal(
                type=SignalType.SHORT,
                symbol=self.config.symbol,
                price=bar["close"],
                confidence=7.0,
                meta={"setup": "orb_breakout", "stop_distance": stop_dist},
            )
        return None

    def ema_pullback(
        self,
        bar: dict[str, Any],
        regime: RegimeType,
        sweep: SweepResult | None,  # noqa: ARG002 - sweep reserved
    ) -> Signal | None:
        """EMA pullback — touch 21 EMA in trend, bounce confirmed by hammer/engulf."""
        if regime != RegimeType.TRENDING:
            return None
        ema_21: float = bar.get("ema_21", 0.0)
        if ema_21 == 0.0:
            return None
        dist = abs(bar["close"] - ema_21) / ema_21
        stop_dist = abs(bar.get("atr_14", 5.0))
        if dist < _EMA_TOUCH_FRAC and bar["close"] > bar["open"]:  # bullish bounce off EMA
            return Signal(
                type=SignalType.LONG,
                symbol=self.config.symbol,
                price=bar["close"],
                confidence=6.5,
                meta={"setup": "ema_pullback", "stop_distance": stop_dist},
            )
        if dist < _EMA_TOUCH_FRAC and bar["close"] < bar["open"]:  # bearish rejection at EMA
            return Signal(
                type=SignalType.SHORT,
                symbol=self.config.symbol,
                price=bar["close"],
                confidence=6.5,
                meta={"setup": "ema_pullback", "stop_distance": stop_dist},
            )
        return None

    def sweep_reclaim(
        self,
        bar: dict[str, Any],
        regime: RegimeType,
        sweep: SweepResult | None,  # noqa: ARG002 - regime reserved
    ) -> Signal | None:
        """Liquidity sweep + reclaim — wick beyond level then close back inside."""
        if sweep is None or not sweep.reclaim_confirmed:
            return None
        stop_dist = abs(bar["close"] - sweep.level) if sweep.level > 0 else abs(bar.get("atr_14", 5.0))
        return Signal(
            type=sweep.direction or SignalType.LONG,
            symbol=self.config.symbol,
            price=bar["close"],
            confidence=8.0,
            meta={"setup": "sweep_reclaim", "sweep_level": sweep.level, "stop_distance": stop_dist},
        )

    def mean_reversion(
        self,
        bar: dict[str, Any],
        regime: RegimeType,
        sweep: SweepResult | None,  # noqa: ARG002 - sweep reserved
    ) -> Signal | None:
        """Mean reversion — extended move from VWAP in ranging regime."""
        if regime != RegimeType.RANGING:
            return None
        vwap: float = bar.get("vwap", 0.0)
        atr: float = bar.get("atr_14", 1.0)
        if vwap == 0.0:
            return None
        dev = (bar["close"] - vwap) / atr
        stop_dist = abs(atr) * 1.2
        if dev < -_MR_Z_ENTRY:
            return Signal(
                type=SignalType.LONG,
                symbol=self.config.symbol,
                price=bar["close"],
                confidence=6.0,
                meta={"setup": "mean_reversion", "stop_distance": stop_dist},
            )
        if dev > _MR_Z_ENTRY:
            return Signal(
                type=SignalType.SHORT,
                symbol=self.config.symbol,
                price=bar["close"],
                confidence=6.0,
                meta={"setup": "mean_reversion", "stop_distance": stop_dist},
            )
        return None
