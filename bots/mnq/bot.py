"""MNQ Futures Bot -- ENGINE tier, 4 setups from APEX v3.

Micro E-mini Nasdaq-100. Tick $0.25, tick value $0.50, point value $2.00. TF: 5m/1m/1s.

Injectable dependencies
-----------------------
* `router`: an ``eta_engine.venues.router.SmartRouter`` instance (or anything
  exposing ``async place_with_failover(OrderRequest) -> OrderResult``). If
  omitted, ``on_signal`` falls back to log-only — preserves the zero-venue
  test/dry-run behavior the fleet relied on before wiring.
* `session_levels`: pre-computed PDH/PDL/ONH/ONL/VWAP anchors. Feed them
  once per day; ``sweep_check`` scans them.
* `tradovate_symbol`: Tradovate contract symbol (e.g. ``MNQH6``). Defaults to
  ``MNQ`` so the spec stays broker-agnostic.
* `strategy_adapter`: optional
  :class:`eta_engine.strategies.engine_adapter.RouterAdapter`. When
  wired, ``on_bar`` first asks the adapter (the six AI-optimized SMC/ICT
  strategies) for a signal; on a miss it falls through to the legacy
  4-setup loop. Leave ``None`` for the pre-v0.1.34 path.

Trailing stop
-------------
``evaluate_exit`` tracks the peak unrealized PnL per position id since entry
and exits when PnL retraces ``trailing_drawdown_r`` R off the peak (default 1R).
A hard stop at ``-risk_per_trade_pct`` R and a hard 2R target remain in place.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from eta_engine.bots.base_bot import (
    BaseBot,
    BotConfig,
    MarginMode,
    Position,
    RegimeType,
    Signal,
    SignalType,
    SweepResult,
    Tier,
)
from eta_engine.venues.base import OrderRequest, OrderResult, OrderStatus, Side

if TYPE_CHECKING:
    from eta_engine.strategies.engine_adapter import RouterAdapter

logger = logging.getLogger(__name__)

MNQ_CONFIG = BotConfig(
    name="MNQ-Engine", symbol="MNQ", tier=Tier.FUTURES, baseline_usd=5500.0,
    starting_capital_usd=5000.0, max_leverage=5.0, risk_per_trade_pct=1.0,
    daily_loss_cap_pct=2.5, max_dd_kill_pct=8.0, margin_mode=MarginMode.CROSS,
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
    """MNQ futures bot — 4 setups, regime-filtered, sweep-aware, router-backed."""

    # Instrument dollars-per-point. Subclasses (NqBot) override with $20.
    POINT_VALUE_USD: float = POINT_VALUE

    def __init__(
        self,
        config: BotConfig | None = None,
        *,
        router: _Router | None = None,
        session_levels: list[float] | None = None,
        tradovate_symbol: str | None = None,
        trailing_drawdown_r: float = _DEFAULT_TRAILING_R,
        strategy_adapter: RouterAdapter | None = None,
    ) -> None:
        super().__init__(config or MNQ_CONFIG)
        self._liquidity_levels: list[float] = list(session_levels or [])
        self._router = router
        self._tradovate_symbol = tradovate_symbol or self.config.symbol
        self._trailing_drawdown_r = trailing_drawdown_r
        self._strategy_adapter = strategy_adapter
        # position_id -> peak unrealized PnL since entry
        self._trailing_peak: dict[str, float] = {}

    # ── Lifecycle ──

    async def start(self) -> None:
        logger.info(
            "MNQ bot starting | capital=$%.2f symbol=%s levels=%d router=%s",
            self.config.starting_capital_usd,
            self._tradovate_symbol,
            len(self._liquidity_levels),
            "yes" if self._router is not None else "no",
        )

    async def stop(self) -> None:
        logger.info("MNQ bot stopping | equity=$%.2f pnl=$%.2f", self.state.equity, self.state.todays_pnl)
        # Clear trailing state so a restarted bot doesn't carry stale peaks.
        self._trailing_peak.clear()

    def load_session_levels(self, levels: list[float]) -> None:
        """Replace current-session liquidity anchors (PDH/PDL/ONH/ONL/VWAP)."""
        self._liquidity_levels = list(levels)
        logger.debug("MNQ levels reloaded: %d anchors", len(self._liquidity_levels))

    # ── Market Events ──

    async def on_bar(self, bar: dict[str, Any]) -> None:
        if not self.check_risk():
            return
        # AI-Optimized strategy stack takes priority when wired.
        if self._strategy_adapter is not None:
            # Propagate bot-state gates into the adapter context.
            self._strategy_adapter.kill_switch_active = self.state.is_killed
            router_signal = self._strategy_adapter.push_bar(bar)
            if router_signal is not None:
                await self.on_signal(router_signal)
                return
        regime = self.regime_filter(bar)
        sweep = self.sweep_check(bar, self._liquidity_levels)
        for setup_fn in (self.orb_breakout, self.ema_pullback, self.sweep_reclaim, self.mean_reversion):
            signal = setup_fn(bar, regime, sweep)
            if signal is not None:
                await self.on_signal(signal)
                break  # one signal per bar

    async def on_signal(self, signal: Signal) -> OrderResult | None:
        """Route a signal to the venue.

        Returns the broker-side ``OrderResult`` when a router is wired, or
        ``None`` in log-only mode. Entry signals convert to market orders
        sized from ``_size_from_signal``; exit signals use ``reduce_only``.
        """
        logger.info("MNQ signal: %s @ %.2f conf=%.1f", signal.type.value, signal.price, signal.confidence)
        if self._router is None:
            return None

        qty = self._size_from_signal(signal)
        if qty <= 0.0:
            logger.debug("MNQ signal skipped: qty=%.4f <= 0", qty)
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
            return None
        if result.status is OrderStatus.REJECTED:
            logger.warning("MNQ order rejected: id=%s", result.order_id)
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
        self, bar: dict[str, Any], regime: RegimeType, sweep: SweepResult | None,  # noqa: ARG002 - regime/sweep reserved for future filters
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
                type=SignalType.LONG, symbol=self.config.symbol, price=bar["close"],
                confidence=7.0, meta={"setup": "orb_breakout", "stop_distance": stop_dist},
            )
        if bar["close"] < orb_low and vol_ok:
            return Signal(
                type=SignalType.SHORT, symbol=self.config.symbol, price=bar["close"],
                confidence=7.0, meta={"setup": "orb_breakout", "stop_distance": stop_dist},
            )
        return None

    def ema_pullback(
        self, bar: dict[str, Any], regime: RegimeType, sweep: SweepResult | None,  # noqa: ARG002 - sweep reserved
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
                type=SignalType.LONG, symbol=self.config.symbol, price=bar["close"],
                confidence=6.5, meta={"setup": "ema_pullback", "stop_distance": stop_dist},
            )
        if dist < _EMA_TOUCH_FRAC and bar["close"] < bar["open"]:  # bearish rejection at EMA
            return Signal(
                type=SignalType.SHORT, symbol=self.config.symbol, price=bar["close"],
                confidence=6.5, meta={"setup": "ema_pullback", "stop_distance": stop_dist},
            )
        return None

    def sweep_reclaim(
        self, bar: dict[str, Any], regime: RegimeType, sweep: SweepResult | None,  # noqa: ARG002 - regime reserved
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
        self, bar: dict[str, Any], regime: RegimeType, sweep: SweepResult | None,  # noqa: ARG002 - sweep reserved
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
                type=SignalType.LONG, symbol=self.config.symbol, price=bar["close"],
                confidence=6.0, meta={"setup": "mean_reversion", "stop_distance": stop_dist},
            )
        if dev > _MR_Z_ENTRY:
            return Signal(
                type=SignalType.SHORT, symbol=self.config.symbol, price=bar["close"],
                confidence=6.0, meta={"setup": "mean_reversion", "stop_distance": stop_dist},
            )
        return None
