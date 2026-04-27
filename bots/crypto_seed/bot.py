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
import statistics
from collections import deque
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, Field

from eta_engine.bots.base_bot import (
    BaseBot,
    BotConfig,
    Fill,
    MarginMode,
    Position,
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
from eta_engine.core.market_quality import (
    build_market_context_summary,
    derive_order_book_metrics,
    format_market_context_summary,
)
from eta_engine.obs.decision_journal import Actor, DecisionJournal, Outcome
from eta_engine.strategies.models import Bar as StrategyBar
from eta_engine.venues.base import OrderRequest, OrderResult, OrderStatus, Side

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from eta_engine.brain.jarvis_context import JarvisContext
    from eta_engine.brain.model_policy import ModelTier, TaskCategory
    from eta_engine.strategies.engine_adapter import RouterAdapter

logger = logging.getLogger(__name__)
SEED_CONFIG = BotConfig(
    name="Crypto-Seed",
    symbol="BTCUSDT",
    tier=Tier.SEED,
    baseline_usd=2000.0,
    starting_capital_usd=2000.0,
    max_leverage=3.0,
    risk_per_trade_pct=0.5,
    daily_loss_cap_pct=3.0,
    max_dd_kill_pct=10.0,
    margin_mode=MarginMode.ISOLATED,
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
    order_id: str | None = None
    status_hint: str = "OPEN"


class GridState(BaseModel):
    levels: list[float] = Field(default_factory=list)
    active_orders: list[GridOrder] = Field(default_factory=list)
    filled_buys: int = 0
    filled_sells: int = 0


class _Router(Protocol):
    async def place_with_failover(self, req: OrderRequest) -> OrderResult: ...


class CryptoSeedBot(BaseBot):
    """BTC grid bot with directional overlay on high confluence.

    JARVIS-ready: pass ``jarvis=JarvisAdmin(...)`` to gate every
    directional overlay ORDER_PLACE through the admin. Grid fills
    are delivered to the orchestrator (``manage_grid`` return value)
    and routed there, so the JARVIS gate in ``on_signal`` covers the
    directional path that represents net-new risk. Pass-through
    ``overnight_explicit=True`` keeps this bot inside the
    CRYPTO_24_7_BOTS overnight whitelist.
    """

    # Audit identity.
    SUBSYSTEM: SubsystemId = SubsystemId.BOT_CRYPTO_SEED

    def __init__(
        self,
        config: BotConfig | None = None,
        *,
        router: _Router | None = None,
        jarvis: JarvisAdmin | None = None,
        journal: DecisionJournal | None = None,
        provide_ctx: Callable[[], JarvisContext] | None = None,
        venue_symbol: str | None = None,
        initial_grid_bounds: tuple[float, float] | None = None,
        strategy_adapter: RouterAdapter | None = None,
        profile: Any | None = None,  # noqa: ANN401 -- profile is a pluggable dataclass
    ) -> None:
        super().__init__(config or SEED_CONFIG)
        self.grid_config = GridConfig()
        self.grid_state = GridState()
        self._router = router
        self._jarvis = jarvis
        self._journal = journal
        self._provide_ctx = provide_ctx
        self._venue_symbol = venue_symbol or self.config.symbol
        self._strategy_adapter = strategy_adapter
        self._profile = profile
        self._recent_bars: deque[StrategyBar] = deque(maxlen=128)
        self._current_bar_idx = 0
        self._loss_streak = 0
        self._throttle_mult = 1.0
        self._risk_lockout_until_bar_idx = -10_000
        self._market_quality = 0.5
        self._execution_quality = 0.5
        self._directional_quality = 0.5
        self._market_quality_label = "UNKNOWN"
        self._market_quality_volume_ratio = 1.0
        self._market_quality_atr_ratio = 1.0
        self._market_quality_body_efficiency = 0.0
        self._market_quality_spread_bps = 0.0
        self._market_quality_book_imbalance = 0.0
        self._market_quality_spread_regime = "UNKNOWN"
        self._last_order_book_venue = ""
        self._last_order_book_depth = 0
        self._last_order_book_age_ms = 0.0
        self._last_order_book_depth_score = 5.0
        self._last_order_book_freshness_score = 5.0
        self._last_order_book_quality = 5.0
        self._last_order_book_quality_bucket = "Q4_6"
        self._last_session_timeframe_key = "UNKNOWN::UNKNOWN"
        self._last_session_timeframe_size_bias = 1.0
        self._last_temporal_size_mult = 1.0
        self._last_session_size_bias = 1.0
        self._last_timeframe_size_bias = 1.0
        self._last_spread_size_bias = 1.0
        self._market_quality_blocked = False
        self._last_session_phase = "UNKNOWN"
        self._last_timeframe_label = "UNKNOWN"
        self._last_timeframe_minutes = 0.0
        self._last_microstructure_score = 5.0
        self._last_pattern_edge_score = 5.0
        if initial_grid_bounds is not None:
            high, low = initial_grid_bounds
            self.init_grid(high, low)

    # ── JARVIS gating helpers ──

    def _ask_jarvis(
        self,
        action: ActionType,
        **payload: Any,  # noqa: ANN401 -- payload is intentionally untyped
    ) -> tuple[bool, float | None, str]:
        """Gate a risk-adding action through JARVIS. Crypto 24/7 so
        ``overnight_explicit=True`` is automatic."""
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
        **metadata: Any,  # noqa: ANN401 -- journal payloads are flexible
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

    # ── Lifecycle ──

    async def start(self) -> None:
        allowed, _cap, code = self._ask_jarvis(
            ActionType.STRATEGY_DEPLOY,
            rationale="arming crypto_seed grid+overlay bot",
            mode="seed_grid",
        )
        if not allowed:
            logger.warning(
                "Crypto Seed bot refused to start: %s",
                code,
            )
            self._record_event(
                intent="crypto_seed_start_blocked",
                rationale=f"jarvis refused STRATEGY_DEPLOY: {code}",
                outcome=Outcome.BLOCKED,
            )
            self.state.is_paused = True
            return
        logger.info(
            "Crypto Seed bot starting | capital=$%.2f symbol=%s grid_levels=%d router=%s jarvis=%s",
            self.config.starting_capital_usd,
            self._venue_symbol,
            len(self.grid_state.levels),
            "yes" if self._router is not None else "no",
            "yes" if self._jarvis is not None else "no",
        )
        self._record_event(
            intent="crypto_seed_start",
            rationale="jarvis approved STRATEGY_DEPLOY" if self._jarvis else "no_jarvis",
            outcome=Outcome.EXECUTED,
            symbol=self._venue_symbol,
            grid_levels=len(self.grid_state.levels),
        )

    async def stop(self) -> None:
        logger.info("Crypto Seed bot stopping | equity=$%.2f", self.state.equity)
        self._record_event(
            intent="crypto_seed_stop",
            rationale="lifecycle.stop",
            outcome=Outcome.NOTED,
            equity=self.state.equity,
        )
        # Mark all grid orders inactive so the orchestrator knows to cancel on-exchange.
        for order in self.grid_state.active_orders:
            order.is_active = False
        self._recent_bars.clear()
        self._current_bar_idx = 0
        self._loss_streak = 0
        self._throttle_mult = 1.0
        self._risk_lockout_until_bar_idx = -10_000
        self._last_order_book_venue = ""
        self._last_order_book_depth = 0
        self._last_order_book_age_ms = 0.0
        self._last_order_book_depth_score = 5.0
        self._last_order_book_freshness_score = 5.0
        self._last_order_book_quality = 5.0
        self._last_order_book_quality_bucket = "Q4_6"

    @staticmethod
    def _as_float(raw: Any, default: float = 0.0) -> float:  # noqa: ANN401 -- raw bar payloads are untyped
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_int(raw: Any, default: int = 0) -> int:  # noqa: ANN401 -- raw bar payloads are untyped
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clamp01(value: float) -> float:
        return max(0.0, min(1.0, value))

    @staticmethod
    def _timeframe_label(minutes: float) -> str:
        if minutes <= 0.0:
            return "UNKNOWN"
        if minutes <= 1.5:
            return "M1"
        if minutes <= 7.5:
            return "M5"
        if minutes <= 22.5:
            return "M15"
        if minutes <= 45.0:
            return "M30"
        if minutes <= 120.0:
            return "H1"
        if minutes <= 360.0:
            return "H4"
        return "D1"

    @staticmethod
    def _session_timeframe_key(session_phase: str, timeframe_label: str) -> str:
        phase = str(session_phase).strip().upper() or "UNKNOWN"
        label = str(timeframe_label).strip().upper() or "UNKNOWN"
        return f"{phase}::{label}"

    def _derive_spread_bps(self, bar: dict[str, Any], close: float) -> float:
        spread_bps = self._as_float(bar.get("spread_bps"), 0.0)
        if spread_bps > 0.0:
            return spread_bps
        bid = self._as_float(bar.get("bid_price"), 0.0) or self._as_float(bar.get("best_bid"), 0.0)
        ask = self._as_float(bar.get("ask_price"), 0.0) or self._as_float(bar.get("best_ask"), 0.0)
        if bid > 0.0 and ask > bid:
            mid = (bid + ask) / 2.0
            if mid > 0.0:
                return max(0.0, ((ask - bid) / mid) * 10_000.0)
        if close > 0.0:
            bb_upper = self._as_float(bar.get("bb_upper"), 0.0)
            bb_lower = self._as_float(bar.get("bb_lower"), 0.0)
            if bb_upper > bb_lower > 0.0:
                return max(0.0, ((bb_upper - bb_lower) / close) * 10_000.0)
        return 0.0

    def _derive_book_imbalance(self, bar: dict[str, Any]) -> float:
        for key in ("book_imbalance", "order_book_imbalance", "bid_ask_imbalance"):
            raw = bar.get(key)
            if raw is None:
                continue
            return self._clamp01((self._as_float(raw, 0.0) + 1.0) / 2.0) * 2.0 - 1.0
        bid_depth = self._as_float(bar.get("bid_depth"), 0.0)
        ask_depth = self._as_float(bar.get("ask_depth"), 0.0)
        if bid_depth > 0.0 and ask_depth > 0.0:
            total = bid_depth + ask_depth
            if total > 0.0:
                return max(-1.0, min(1.0, (bid_depth - ask_depth) / total))
        return 0.0

    @staticmethod
    def _spread_regime_label(
        spread_bps: float,
        book_imbalance: float,
        raw: Any | None = None,  # noqa: ANN401 -- raw comes from upstream bar dicts
    ) -> str:
        if isinstance(raw, str):
            label = raw.strip().upper()
            if label in {"TIGHT", "NORMAL", "WIDE", "STRESSED"}:
                return label
        abs_imb = abs(book_imbalance)
        if spread_bps <= 0.0 and abs_imb <= 0.0:
            return "UNKNOWN"
        if spread_bps <= 1.5 and abs_imb <= 0.20:
            return "TIGHT"
        if spread_bps <= 4.5 and abs_imb <= 0.40:
            return "NORMAL"
        if spread_bps <= 12.0 and abs_imb <= 0.65:
            return "WIDE"
        return "STRESSED"

    def _quality_signal_mult(self, score: float) -> float:
        return max(
            0.55,
            min(1.5, 0.55 + score * (1.0 - 0.55)),
        )

    def _profile_bias(self, field_name: str, key: str) -> float:
        if not key or key == "UNKNOWN":
            return 1.0
        profile = self._profile
        if profile is None:
            return 1.0
        raw_map: Any
        raw_map = profile.get(field_name, {}) if isinstance(profile, dict) else getattr(profile, field_name, {})
        if not isinstance(raw_map, dict):
            return 1.0
        try:
            value = float(raw_map.get(key, 1.0))
        except (TypeError, ValueError):
            value = 1.0
        return max(0.5, min(1.5, value))

    def _profile_float(self, field_name: str, default: float) -> float:
        profile = self._profile
        if profile is None:
            return default
        raw: Any
        raw = profile.get(field_name, default) if isinstance(profile, dict) else getattr(profile, field_name, default)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def _temporal_size_mult(
        self,
        *,
        session_phase: str | None = None,
        timeframe_label: str | None = None,
        microstructure_score: float | None = None,
        spread_regime: str | None = None,
        pattern_edge_score: float | None = None,
        order_book_quality: float | None = None,
        order_book_freshness_score: float | None = None,
        order_book_quality_bucket: str | None = None,
    ) -> float:
        session_bias = self._profile_bias("session_phase_size_bias", session_phase or self._last_session_phase)
        timeframe_bias = self._profile_bias("timeframe_size_bias", timeframe_label or self._last_timeframe_label)
        session_timeframe_bias = self._profile_bias(
            "session_timeframe_size_bias",
            self._session_timeframe_key(  # noqa: E501 -- key builder call kept inline for clarity
                session_phase or self._last_session_phase,
                timeframe_label or self._last_timeframe_label,
            ),
        )
        spread_bias = self._profile_bias(  # noqa: E501 -- one-liner fallback chain
            "spread_regime_size_bias",
            spread_regime or self._market_quality_spread_regime,
        )
        micro = max(
            0.92,
            min(
                1.08,
                0.92
                + max(
                    0.0,
                    min(  # noqa: E501 -- microstructure clamp formula kept inline
                        10.0,
                        microstructure_score if microstructure_score is not None else self._last_microstructure_score,
                    ),
                )
                * 0.016,
            ),
        )
        edge = max(
            0.90,
            min(
                1.10,
                0.90
                + max(
                    0.0,
                    min(10.0, pattern_edge_score if pattern_edge_score is not None else self._last_pattern_edge_score),
                )
                * 0.020,
            ),
        )
        book_quality = max(
            0.92,
            min(
                1.08,
                0.92
                + max(
                    0.0,
                    min(10.0, order_book_quality if order_book_quality is not None else self._last_order_book_quality),
                )
                * 0.016,
            ),
        )
        quality_bucket_bias = self._profile_bias(
            "order_book_quality_size_bias",
            order_book_quality_bucket or self._last_order_book_quality_bucket,
        )
        freshness = max(
            0.94,
            min(
                1.06,
                0.94
                + max(
                    0.0,
                    min(
                        10.0,
                        order_book_freshness_score
                        if order_book_freshness_score is not None
                        else self._last_order_book_freshness_score,
                    ),
                )
                * 0.012,
            ),
        )
        combined = (
            session_bias
            * timeframe_bias
            * session_timeframe_bias
            * spread_bias
            * micro
            * edge
            * book_quality
            * freshness
            * quality_bucket_bias
        )
        return max(0.65, min(1.35, combined))

    def _pattern_edge_score(
        self,
        *,
        session_phase: str,
        timeframe_label: str,
        microstructure_score: float,
        spread_bps: float,
        book_imbalance: float,
        spread_regime: str,
        order_book_depth_score: float,
        order_book_freshness_score: float,
        order_book_quality: float,
        order_book_quality_bucket: str,
    ) -> float:
        session_fit = {
            "OVERNIGHT": 1.00,
            "PREMARKET": 1.04,
            "OPEN_DRIVE": 1.10,
            "MORNING": 1.05,
            "LUNCH": 0.92,
            "AFTERNOON": 1.08,
            "CLOSE": 1.04,
        }.get(session_phase, 1.0)
        session_timeframe_fit = self._profile_bias(
            "session_timeframe_edge_bias",
            self._session_timeframe_key(session_phase, timeframe_label),
        )
        timeframe_fit = {
            "M1": 1.08,
            "M5": 1.10,
            "M15": 1.10,
            "M30": 1.03,
            "H1": 0.98,
            "H4": 0.94,
            "D1": 0.90,
        }.get(timeframe_label, 1.0)
        spread_fit = {
            "TIGHT": 1.06,
            "NORMAL": 1.00,
            "WIDE": 0.96,
            "STRESSED": 0.90,
        }.get(self._spread_regime_label(spread_bps, book_imbalance, spread_regime), 1.0)
        book_fit = max(0.90, min(1.10, 1.0 - abs(book_imbalance) * 0.2))
        micro_fit = max(0.85, min(1.15, 0.85 + max(0.0, min(10.0, microstructure_score)) * 0.03))
        depth_fit = max(0.90, min(1.10, 0.90 + max(0.0, min(10.0, order_book_depth_score)) * 0.02))
        freshness_fit = max(0.92, min(1.08, 0.92 + max(0.0, min(10.0, order_book_freshness_score)) * 0.016))
        quality_fit = max(0.90, min(1.10, 0.90 + max(0.0, min(10.0, order_book_quality)) * 0.018))
        quality_bucket_fit = self._profile_bias("order_book_quality_edge_bias", order_book_quality_bucket)
        quality_bucket_fit = max(
            0.90,
            min(
                1.10,
                quality_bucket_fit
                * {
                    "Q0_2": 0.94,
                    "Q2_4": 0.98,
                    "Q4_6": 1.00,
                    "Q6_8": 1.04,
                    "Q8_10": 1.08,
                }.get(order_book_quality_bucket, 1.0),
            ),
        )
        combined = (
            5.0
            * session_fit
            * timeframe_fit
            * session_timeframe_fit
            * micro_fit
            * spread_fit
            * book_fit
            * depth_fit
            * freshness_fit
            * quality_fit
            * quality_bucket_fit
        )
        return max(0.0, min(10.0, combined))

    def _quality_block_reason(self) -> str:
        reasons: list[str] = []
        if self._execution_quality < 0.40:
            reasons.append(f"execution={self._execution_quality:.2f}<0.40")
        if self._directional_quality < 0.50:
            reasons.append(f"directional={self._directional_quality:.2f}<0.50")
        if self._market_quality_label == "THIN":
            reasons.append("market=THIN")
        ob_floor = self._profile_float("order_book_quality_floor", 4.25)
        if self._last_order_book_quality < ob_floor:
            reasons.append(
                f"order_book={self._last_order_book_quality:.2f}<floor={ob_floor:.2f}",
            )
        return ", ".join(reasons) if reasons else "quality gate open"

    def _append_recent_bar(self, bar: dict[str, Any]) -> None:
        ts = self._as_int(bar.get("ts") or bar.get("timestamp") or bar.get("bar_idx"), 0)
        self._recent_bars.append(
            StrategyBar(
                ts=ts,
                open=self._as_float(bar.get("open"), self._as_float(bar.get("close"), 0.0)),
                high=self._as_float(bar.get("high"), self._as_float(bar.get("close"), 0.0)),
                low=self._as_float(bar.get("low"), self._as_float(bar.get("close"), 0.0)),
                close=self._as_float(bar.get("close"), 0.0),
                volume=self._as_float(bar.get("volume", bar.get("vol")), 0.0),
            ),
        )

    def _refresh_tape_quality(self, bar: dict[str, Any]) -> None:
        bar = self._bar_with_fallbacks(bar)
        close = self._as_float(bar.get("close"), 0.0)
        open_ = self._as_float(bar.get("open"), close)
        high = self._as_float(bar.get("high"), close)
        low = self._as_float(bar.get("low"), close)
        body = abs(close - open_)
        candle_range = max(high - low, 0.0)
        body_efficiency = self._clamp01(body / candle_range) if candle_range > 0.0 else 0.0

        volumes = [float(item.volume) for item in list(self._recent_bars)[-50:] if float(item.volume) >= 0.0]
        avg_volume = self._as_float(bar.get("avg_volume"), 0.0)
        if avg_volume <= 0.0 and volumes:
            avg_volume = statistics.fmean(volumes[-20:])
        latest_volume = self._as_float(bar.get("volume"), volumes[-1] if volumes else 0.0)
        volume_ratio = latest_volume / max(avg_volume, 1e-9) if avg_volume > 0.0 else 1.0
        volume_score = self._clamp01((volume_ratio - 0.55) / 1.25)

        atr_14 = self._as_float(bar.get("atr_14"), 0.0)
        avg_atr_50 = self._as_float(bar.get("avg_atr_50"), 0.0)
        atr_ratio = atr_14 / max(avg_atr_50, 1e-9) if atr_14 > 0.0 and avg_atr_50 > 0.0 else 1.0
        atr_score = self._clamp01(1.0 - max(0.0, atr_ratio - 1.0) * 0.55)

        ema_9 = self._as_float(bar.get("ema_9"), 0.0)
        ema_21 = self._as_float(bar.get("ema_21"), 0.0)
        ema_gap_pct = abs(ema_9 - ema_21) / max(close, 1.0) if close > 0.0 and ema_9 > 0.0 and ema_21 > 0.0 else 0.0
        ema_gap_score = self._clamp01(ema_gap_pct / max(0.004 * 2.5, 1e-6))

        adx = self._as_float(bar.get("adx_14"), 0.0)
        adx_score = self._clamp01((adx - 23.0) / max(30.0 - 23.0, 1e-9))

        spread_bps = self._derive_spread_bps(bar, close)
        spread_score = 0.65 if spread_bps <= 0.0 else self._clamp01(1.0 - spread_bps / 18.0)
        book_imbalance = self._derive_book_imbalance(bar)
        spread_regime = self._spread_regime_label(spread_bps, book_imbalance, bar.get("spread_regime"))
        balance_score = self._clamp01(1.0 - abs(book_imbalance))
        order_book_metrics = derive_order_book_metrics(
            bar,
            bar_ts=bar.get("ts"),
            spread_bps=spread_bps,
            book_imbalance=book_imbalance,
        )
        order_book_depth_score = order_book_metrics["order_book_depth_score"]
        order_book_freshness_score = order_book_metrics["order_book_freshness_score"]
        order_book_quality = order_book_metrics["order_book_quality"]
        order_book_quality_bucket = str(order_book_metrics["order_book_quality_bucket"])
        depth_score = self._clamp01(order_book_depth_score / 10.0)
        freshness_score = self._clamp01(order_book_freshness_score / 10.0)
        quality_score = self._clamp01(order_book_quality / 10.0)

        execution_quality = self._clamp01(
            0.30 * volume_score
            + 0.20 * body_efficiency
            + 0.14 * atr_score
            + 0.10 * spread_score
            + 0.08 * balance_score
            + 0.18 * quality_score,
        )
        directional_quality = self._clamp01(
            0.36 * adx_score
            + 0.24 * ema_gap_score
            + 0.14 * volume_score
            + 0.10 * (0.5 + 0.5 * abs(book_imbalance))
            + 0.08 * depth_score
            + 0.08 * freshness_score,
        )
        market_quality = self._clamp01(
            0.48 * execution_quality + 0.34 * directional_quality + 0.18 * quality_score,
        )
        if market_quality >= 0.75 and execution_quality >= 0.70 and directional_quality >= 0.70:
            label = "HEAVY"
        elif market_quality >= 0.60 and execution_quality >= 0.60:
            label = "LIQUID"
        elif directional_quality >= 0.60 and adx >= 30.0:
            label = "TREND"
        elif market_quality < 0.40:
            label = "THIN"
        else:
            label = "NORMAL"
        self._market_quality = market_quality
        self._execution_quality = execution_quality
        self._directional_quality = directional_quality
        self._market_quality_label = label
        self._market_quality_volume_ratio = volume_ratio
        self._market_quality_atr_ratio = atr_ratio
        self._market_quality_body_efficiency = body_efficiency
        self._market_quality_spread_bps = spread_bps
        self._market_quality_book_imbalance = book_imbalance
        self._market_quality_spread_regime = spread_regime
        self._last_order_book_venue = str(  # noqa: E501 -- one-line venue fallback chain
            bar.get("order_book_venue") or bar.get("venue") or self._last_order_book_venue or "",
        )
        try:
            self._last_order_book_depth = int(
                float(  # noqa: E501 -- one-line depth fallback chain
                    bar.get("order_book_depth") or self._last_order_book_depth or 0,
                )
            )
        except (TypeError, ValueError):
            self._last_order_book_depth = int(self._last_order_book_depth or 0)
        self._last_order_book_age_ms = float(  # noqa: E501 -- one-line age fallback chain
            bar.get("order_book_age_ms") or order_book_metrics["order_book_age_ms"] or 0.0,
        )
        self._last_order_book_depth_score = order_book_depth_score
        self._last_order_book_freshness_score = order_book_freshness_score
        self._last_order_book_quality = order_book_quality
        self._last_order_book_quality_bucket = order_book_quality_bucket
        self._market_quality_blocked = (
            execution_quality < 0.40
            or directional_quality < 0.50
            or label == "THIN"
            or order_book_quality < self._profile_float("order_book_quality_floor", 4.25)
        )
        session_phase = str(bar.get("session_phase", "UNKNOWN")).strip().upper() or "UNKNOWN"
        timeframe_minutes = self._as_float(bar.get("timeframe_minutes"), 0.0)
        timeframe_label = str(bar.get("timeframe_label", self._timeframe_label(timeframe_minutes))).strip().upper()
        if timeframe_label == "UNKNOWN":
            timeframe_label = self._timeframe_label(timeframe_minutes)
        session_timeframe_key = self._session_timeframe_key(session_phase, timeframe_label)
        microstructure_score = self._as_float(
            bar.get("microstructure_score"),
            max(0.0, min(10.0, self._market_quality * 10.0)),
        )
        pattern_edge_score = self._as_float(
            bar.get("pattern_edge_score"),
            self._pattern_edge_score(
                session_phase=session_phase,
                timeframe_label=timeframe_label,
                microstructure_score=microstructure_score,
                spread_bps=spread_bps,
                book_imbalance=book_imbalance,
                spread_regime=spread_regime,
                order_book_depth_score=order_book_depth_score,
                order_book_freshness_score=order_book_freshness_score,
                order_book_quality=order_book_quality,
                order_book_quality_bucket=order_book_quality_bucket,
            ),
        )
        self._last_session_phase = session_phase
        self._last_timeframe_label = timeframe_label
        self._last_session_timeframe_key = session_timeframe_key
        self._last_timeframe_minutes = timeframe_minutes
        self._last_microstructure_score = microstructure_score
        self._last_pattern_edge_score = pattern_edge_score
        self._last_session_size_bias = self._profile_bias(
            "session_phase_size_bias",
            session_phase,
        )
        self._last_timeframe_size_bias = self._profile_bias(
            "timeframe_size_bias",
            timeframe_label,
        )
        self._last_session_timeframe_size_bias = self._profile_bias(
            "session_timeframe_size_bias",
            session_timeframe_key,
        )
        self._last_spread_size_bias = self._profile_bias(
            "spread_regime_size_bias",
            spread_regime,
        )
        self._last_temporal_size_mult = self._temporal_size_mult(
            session_phase=session_phase,
            timeframe_label=timeframe_label,
            microstructure_score=microstructure_score,
            spread_regime=spread_regime,
            pattern_edge_score=pattern_edge_score,
            order_book_quality=order_book_quality,
            order_book_freshness_score=order_book_freshness_score,
            order_book_quality_bucket=order_book_quality_bucket,
        )

    def seed_history(self, bars: Iterable[dict[str, Any]]) -> None:
        self._recent_bars.clear()
        self._current_bar_idx = 0
        for bar in bars:
            self._append_recent_bar(bar)
            self._current_bar_idx = self._as_int(bar.get("bar_idx"), self._current_bar_idx + 1)

    def _bar_index_for(self, bar: dict[str, Any]) -> int:
        raw_idx = bar.get("bar_idx")
        if raw_idx is None:
            self._current_bar_idx += 1
            return self._current_bar_idx
        idx = self._as_int(raw_idx, self._current_bar_idx + 1)
        self._current_bar_idx = idx
        return idx

    def _bar_with_fallbacks(self, bar: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(bar)
        closes = [float(item.close) for item in list(self._recent_bars)[-50:] if float(item.close) > 0.0]
        volumes = [float(item.volume) for item in list(self._recent_bars)[-50:] if float(item.volume) >= 0.0]
        if closes:
            if self._as_float(enriched.get("ema_9"), 0.0) <= 0.0 and len(closes) >= 2:
                enriched["ema_9"] = self._ema(closes[-9:], 9)
            if self._as_float(enriched.get("ema_21"), 0.0) <= 0.0 and len(closes) >= 2:
                enriched["ema_21"] = self._ema(closes[-21:], 21)
            if self._as_float(enriched.get("adx_14"), 0.0) <= 0.0:
                enriched["adx_14"] = self._derived_adx(closes[-14:])
            if self._as_float(enriched.get("confluence_score"), 0.0) <= 0.0:
                ema_9 = self._as_float(enriched.get("ema_9"), 0.0)
                ema_21 = self._as_float(enriched.get("ema_21"), 0.0)
                adx = self._as_float(enriched.get("adx_14"), 0.0)
                trend_gap = 0.0
                if ema_9 > 0.0 and ema_21 > 0.0:
                    trend_gap = abs(ema_9 - ema_21) / max(ema_21, 1.0) * 100.0
                avg_volume = self._as_float(enriched.get("avg_volume"), 0.0)
                if avg_volume <= 0.0 and volumes:
                    avg_volume = statistics.fmean(volumes[-20:])
                    enriched["avg_volume"] = avg_volume
                latest_volume = self._as_float(enriched.get("volume"), volumes[-1] if volumes else 0.0)
                vol_ratio = latest_volume / avg_volume if avg_volume > 0.0 else 0.0
                enriched["confluence_score"] = round(
                    min(10.0, 5.0 + adx * 0.12 + min(3.0, trend_gap * 0.5) + max(0.0, vol_ratio - 1.0)),
                    3,
                )
        if self._as_float(enriched.get("avg_volume"), 0.0) <= 0.0 and volumes:
            enriched["avg_volume"] = statistics.fmean(volumes[-20:])
        if self._as_float(enriched.get("atr_14"), 0.0) <= 0.0 and closes:
            ranges = [abs(closes[idx] - closes[idx - 1]) for idx in range(1, len(closes))]
            if ranges:
                enriched["atr_14"] = statistics.fmean(ranges[-14:])
        if self._as_float(enriched.get("avg_atr_50"), 0.0) <= 0.0 and closes:
            ranges = [abs(closes[idx] - closes[idx - 1]) for idx in range(1, len(closes))]
            if ranges:
                enriched["avg_atr_50"] = statistics.fmean(ranges[-50:])
        if (
            self._as_float(enriched.get("bb_upper"), 0.0) <= 0.0 or self._as_float(enriched.get("bb_lower"), 0.0) <= 0.0
        ) and len(closes) >= 5:
            tail = closes[-20:]
            center = statistics.fmean(tail)
            spread = statistics.pstdev(tail) if len(tail) > 1 else 0.0
            width = max(center * 0.0015, spread * 2.0)
            if self._as_float(enriched.get("bb_upper"), 0.0) <= 0.0:
                enriched["bb_upper"] = center + width
            if self._as_float(enriched.get("bb_lower"), 0.0) <= 0.0:
                enriched["bb_lower"] = center - width
        return enriched

    @staticmethod
    def _ema(values: list[float], span: int) -> float:
        if not values:
            return 0.0
        alpha = 2.0 / (float(span) + 1.0)
        ema = float(values[0])
        for value in values[1:]:
            ema = alpha * float(value) + (1.0 - alpha) * ema
        return ema

    @staticmethod
    def _derived_adx(values: list[float]) -> float:
        if len(values) < 3:
            return 0.0
        total_move = sum(abs(values[idx] - values[idx - 1]) for idx in range(1, len(values)))
        if total_move <= 0.0:
            return 0.0
        net_move = abs(values[-1] - values[0])
        return max(0.0, min(50.0, 100.0 * net_move / total_move))

    def _refresh_runtime_throttle(self, bar: dict[str, Any]) -> None:
        bar = self._bar_with_fallbacks(bar)
        atr_14 = self._as_float(bar.get("atr_14"), 0.0)
        avg_atr_50 = self._as_float(bar.get("avg_atr_50"), 0.0)
        vol_mult = 1.0
        if atr_14 > 0.0 and avg_atr_50 > 0.0:
            ratio = atr_14 / avg_atr_50
            if ratio > 1.6:
                stretch = min(1.0, (ratio - 1.6) / 1.6)
                vol_mult = 1.0 - stretch * 0.4
        loss_mult = 1.0 - (self._loss_streak * 0.12)
        self._throttle_mult = max(0.5, min(1.0, vol_mult, max(0.5, loss_mult)))

    def _risk_lockout_active(self) -> bool:
        return self._risk_lockout_until_bar_idx > self._current_bar_idx

    def _enter_loss_lockout(self) -> None:
        if self._loss_streak < 3:
            return
        self._risk_lockout_until_bar_idx = max(self._risk_lockout_until_bar_idx, self._current_bar_idx + 6)

    def _refresh_risk_lockout(self) -> None:
        if self._risk_lockout_until_bar_idx <= -10_000:
            return
        if self._risk_lockout_until_bar_idx <= self._current_bar_idx:
            self._risk_lockout_until_bar_idx = -10_000

    def _inventory_fill_imbalance(self) -> int:
        return self.grid_state.filled_buys - self.grid_state.filled_sells

    def _inventory_side_mult(self, side: Side) -> float:
        imbalance = self._inventory_fill_imbalance()
        if imbalance == 0:
            return 1.0
        if side == Side.BUY and imbalance > 0:
            return max(0.55, 1.0 - imbalance * 0.1)
        if side == Side.SELL and imbalance < 0:
            return max(0.55, 1.0 - abs(imbalance) * 0.1)
        return 1.0

    def _inventory_signal_mult(self, signal: Signal) -> float:
        side = Side.BUY if signal.type is SignalType.LONG else Side.SELL
        return self._inventory_side_mult(side)

    @property
    def runtime_snapshot(self) -> dict[str, Any]:
        snapshot = {
            "mode": "SEED",
            "current_bar_idx": self._current_bar_idx,
            "loss_streak": self._loss_streak,
            "throttle_mult": round(self._throttle_mult, 4),
            "grid_level_count": len(self.grid_state.levels),
            "grid_armed": sum(1 for order in self.grid_state.active_orders if order.is_active),
            "grid_filled_buys": self.grid_state.filled_buys,
            "grid_filled_sells": self.grid_state.filled_sells,
            "grid_fill_imbalance": self._inventory_fill_imbalance(),
            "risk_lockout_active": self._risk_lockout_active(),
            "risk_lockout_remaining_bars": max(0, self._risk_lockout_until_bar_idx - self._current_bar_idx)
            if self._risk_lockout_active()
            else 0,
            "market_quality": round(self._market_quality, 4),
            "execution_quality": round(self._execution_quality, 4),
            "directional_quality": round(self._directional_quality, 4),
            "market_quality_label": self._market_quality_label,
            "market_quality_volume_ratio": round(self._market_quality_volume_ratio, 4),
            "market_quality_atr_ratio": round(self._market_quality_atr_ratio, 4),
            "market_quality_body_efficiency": round(self._market_quality_body_efficiency, 4),
            "market_quality_spread_bps": round(self._market_quality_spread_bps, 4),
            "market_quality_book_imbalance": round(self._market_quality_book_imbalance, 4),
            "spread_bps": round(self._market_quality_spread_bps, 4),
            "book_imbalance": round(self._market_quality_book_imbalance, 4),
            "spread_regime": self._market_quality_spread_regime,
            "market_quality_blocked": self._market_quality_blocked,
            "market_context_asset": self._venue_symbol,
            "market_context_venue": self._last_order_book_venue or None,
            "order_book_venue": self._last_order_book_venue or None,
            "order_book_depth": self._last_order_book_depth,
            "order_book_age_ms": round(self._last_order_book_age_ms, 4),
            "order_book_depth_score": round(self._last_order_book_depth_score, 4),
            "order_book_freshness_score": round(self._last_order_book_freshness_score, 4),
            "order_book_quality": round(self._last_order_book_quality, 4),
            "order_book_quality_bucket": self._last_order_book_quality_bucket,
            "temporal_size_mult": round(self._last_temporal_size_mult, 4),
            "session_size_bias": round(self._last_session_size_bias, 4),
            "timeframe_size_bias": round(self._last_timeframe_size_bias, 4),
            "session_timeframe_size_bias": round(self._last_session_timeframe_size_bias, 4),
            "spread_size_bias": round(self._last_spread_size_bias, 4),
            "session_phase": self._last_session_phase,
            "timeframe_label": self._last_timeframe_label,
            "session_timeframe_key": self._last_session_timeframe_key,
            "timeframe_minutes": round(self._last_timeframe_minutes, 4),
            "microstructure_score": round(self._last_microstructure_score, 4),
            "pattern_edge_score": round(self._last_pattern_edge_score, 4),
            "recent_bar_count": len(self._recent_bars),
        }
        market_context_summary = build_market_context_summary(snapshot)
        if market_context_summary:
            snapshot["market_context_summary"] = market_context_summary
            snapshot["market_context_summary_text"] = format_market_context_summary(market_context_summary)
        return snapshot

    # ── Grid Math ──

    @staticmethod
    def calculate_grid_levels(high: float, low: float, n: int) -> list[float]:
        """Geometric grid: ratio r = (high/low)^(1/n), levels = low * r^i."""
        if high <= low or n <= 0:
            return []
        r = (high / low) ** (1.0 / n)
        return [low * (r**i) for i in range(n + 1)]

    def init_grid(self, price_high: float, price_low: float) -> None:
        """Set grid bounds and compute levels + per-level capital."""
        self.grid_config.price_high = price_high
        self.grid_config.price_low = price_low
        levels = self.calculate_grid_levels(price_high, price_low, self.grid_config.n_levels)
        self.grid_state.levels = levels
        usable = self.state.equity * 0.90  # 10% reserve
        self.grid_config.capital_per_level = usable / max(len(levels), 1)
        self._loss_streak = 0
        self._throttle_mult = 1.0
        self._risk_lockout_until_bar_idx = -10_000

    def manage_grid(self, current_price: float, grid_state: GridState) -> list[GridOrder]:
        """Evaluate grid vs current price, return orders to place/cancel."""
        orders: list[GridOrder] = []
        temporal_mult = self._last_temporal_size_mult
        for i, lvl in enumerate(grid_state.levels):
            size = self.grid_config.capital_per_level / lvl if lvl > 0 else 0.0
            side = Side.BUY if lvl < current_price else Side.SELL
            orders.append(
                GridOrder(
                    level_idx=i,
                    price=lvl,
                    side=side.value,
                    size=(
                        size
                        * self._inventory_side_mult(side)
                        * self._quality_signal_mult(self._execution_quality)
                        * max(0.85, min(1.15, 0.85 + self._last_order_book_quality * 0.015))
                        * temporal_mult
                    ),
                ),
            )
        return orders

    def _refresh_grid_snapshot(self, current_price: float) -> None:
        snapshot = self.manage_grid(current_price, self.grid_state)
        active_by_key = {
            (float(order.price), self._coerce_side(order.side)): order for order in self.grid_state.active_orders
        }
        for order in snapshot:
            previous = active_by_key.get((float(order.price), self._coerce_side(order.side)))
            if previous is None:
                continue
            if not previous.is_active:
                order.is_active = False
                order.order_id = previous.order_id
                order.status_hint = previous.status_hint
        self.grid_state.active_orders = snapshot

    @staticmethod
    def _coerce_side(raw: str | Side | None) -> Side | None:
        if raw is None:
            return None
        if isinstance(raw, Side):
            return raw
        try:
            return Side(str(raw).upper())
        except ValueError:
            return None

    # ── Directional Overlay ──

    def directional_overlay(self, bar: dict[str, Any], confluence: float) -> Signal | None:
        """Directional trade on top of grid when confluence > 7."""
        bar = self._bar_with_fallbacks(bar)
        if self._directional_quality < 0.50:
            return None
        if confluence <= 7.0:
            return None
        ema_fast: float = bar.get("ema_9", 0.0)
        ema_slow: float = bar.get("ema_21", 0.0)
        if ema_fast == 0.0 or ema_slow == 0.0:
            return None
        session_phase = str(bar.get("session_phase", self._last_session_phase)).strip().upper() or "UNKNOWN"
        timeframe_minutes = self._as_float(bar.get("timeframe_minutes"), self._last_timeframe_minutes)
        timeframe_label = (
            str(
                bar.get("timeframe_label", self._timeframe_label(timeframe_minutes)),
            )
            .strip()
            .upper()
        )
        if timeframe_label == "UNKNOWN":
            timeframe_label = self._timeframe_label(timeframe_minutes)
        session_timeframe_key = self._session_timeframe_key(session_phase, timeframe_label)
        microstructure_score = self._as_float(
            bar.get("microstructure_score"),
            self._last_microstructure_score,
        )
        spread_bps = self._derive_spread_bps(bar, self._as_float(bar.get("close"), 0.0))
        book_imbalance = self._derive_book_imbalance(bar)
        spread_regime = self._spread_regime_label(spread_bps, book_imbalance, bar.get("spread_regime"))
        order_book_metrics = derive_order_book_metrics(
            bar,
            bar_ts=bar.get("ts"),
            spread_bps=spread_bps,
            book_imbalance=book_imbalance,
        )
        order_book_depth_score = order_book_metrics["order_book_depth_score"]
        order_book_freshness_score = order_book_metrics["order_book_freshness_score"]
        order_book_quality = order_book_metrics["order_book_quality"]
        order_book_quality_bucket = str(order_book_metrics["order_book_quality_bucket"])
        session_timeframe_fit = self._profile_bias("session_timeframe_edge_bias", session_timeframe_key)
        pattern_edge_score = self._pattern_edge_score(
            session_phase=session_phase,
            timeframe_label=timeframe_label,
            microstructure_score=microstructure_score,
            spread_bps=spread_bps,
            book_imbalance=book_imbalance,
            spread_regime=spread_regime,
            order_book_depth_score=order_book_depth_score,
            order_book_freshness_score=order_book_freshness_score,
            order_book_quality=order_book_quality,
            order_book_quality_bucket=order_book_quality_bucket,
        )
        temporal_fit = max(0.85, min(1.15, 0.85 + pattern_edge_score * 0.03 * session_timeframe_fit))
        temporal_size_mult = self._temporal_size_mult(
            session_phase=session_phase,
            timeframe_label=timeframe_label,
            microstructure_score=microstructure_score,
            spread_regime=spread_regime,
            pattern_edge_score=pattern_edge_score,
            order_book_quality=order_book_quality,
            order_book_freshness_score=order_book_freshness_score,
            order_book_quality_bucket=order_book_quality_bucket,
        )
        if ema_fast > ema_slow:
            return Signal(
                type=SignalType.LONG,
                symbol=self.config.symbol,
                price=bar["close"],
                confidence=min(
                    10.0,
                    (confluence + max(0.0, (self._directional_quality - 0.50) * 4.0)) * temporal_fit,
                ),
                meta={
                    "size_mult": self._quality_signal_mult(self._directional_quality),
                    "temporal_size_mult": temporal_size_mult,
                    "session_phase": session_phase,
                    "timeframe_label": timeframe_label,
                    "session_timeframe_key": session_timeframe_key,
                    "timeframe_minutes": timeframe_minutes,
                    "microstructure_score": microstructure_score,
                    "pattern_edge_score": pattern_edge_score,
                    "spread_bps": spread_bps,
                    "book_imbalance": book_imbalance,
                    "spread_regime": spread_regime,
                    "order_book_venue": bar.get("order_book_venue"),
                    "order_book_depth": bar.get("order_book_depth"),
                    "order_book_age_ms": order_book_metrics["order_book_age_ms"],
                    "order_book_depth_score": order_book_depth_score,
                    "order_book_freshness_score": order_book_freshness_score,
                    "order_book_quality": order_book_quality,
                    "order_book_quality_bucket": order_book_quality_bucket,
                    "temporal_fit": temporal_fit,
                },
            )
        if ema_fast < ema_slow:
            return Signal(
                type=SignalType.SHORT,
                symbol=self.config.symbol,
                price=bar["close"],
                confidence=min(
                    10.0,
                    (confluence + max(0.0, (self._directional_quality - 0.50) * 4.0)) * temporal_fit,
                ),
                meta={
                    "size_mult": self._quality_signal_mult(self._directional_quality),
                    "temporal_size_mult": temporal_size_mult,
                    "session_phase": session_phase,
                    "timeframe_label": timeframe_label,
                    "session_timeframe_key": session_timeframe_key,
                    "timeframe_minutes": timeframe_minutes,
                    "microstructure_score": microstructure_score,
                    "pattern_edge_score": pattern_edge_score,
                    "spread_bps": spread_bps,
                    "book_imbalance": book_imbalance,
                    "spread_regime": spread_regime,
                    "order_book_venue": bar.get("order_book_venue"),
                    "order_book_depth": bar.get("order_book_depth"),
                    "order_book_age_ms": order_book_metrics["order_book_age_ms"],
                    "order_book_depth_score": order_book_depth_score,
                    "order_book_freshness_score": order_book_freshness_score,
                    "order_book_quality": order_book_quality,
                    "order_book_quality_bucket": order_book_quality_bucket,
                    "temporal_fit": temporal_fit,
                },
            )
        return None

    # ── Market Events ──

    async def on_bar(self, bar: dict[str, Any]) -> None:
        # Wave-6 sage plumbing (2026-04-27): rolling sage-bar buffer.
        self.observe_bar_for_sage(bar)
        if not self.check_risk():
            return
        self._bar_index_for(bar)
        self._append_recent_bar(bar)
        bar = self._bar_with_fallbacks(bar)
        self._refresh_tape_quality(bar)
        self._refresh_runtime_throttle(bar)
        self._refresh_risk_lockout()
        # Orchestrator drains grid_orders at its own cadence (see scripts/run_eta_live).
        self._refresh_grid_snapshot(bar["close"])
        # AI-Optimized strategy stack takes priority when wired.
        if self._strategy_adapter is not None:
            self._strategy_adapter.kill_switch_active = self.state.is_killed
            router_signal = self._strategy_adapter.push_bar(bar)
            if router_signal is not None:
                await self.on_signal(router_signal)
                return
        if self._risk_lockout_active():
            return
        confluence: float = bar.get("confluence_score", 0.0)
        signal = self.directional_overlay(bar, confluence)
        if signal:
            await self.on_signal(signal)

    async def on_signal(self, signal: Signal) -> OrderResult | None:
        """Route the directional-overlay signal through JARVIS.

        Grid fills are drained separately via ``manage_grid`` -- the
        gate here only covers the directional overlay which represents
        net-new risk outside the grid envelope.
        """
        logger.info("Seed signal: %s @ %.2f conf=%.1f", signal.type.value, signal.price, signal.confidence)
        if signal.type in {SignalType.LONG, SignalType.SHORT} and self._risk_lockout_active():
            self._record_event(
                intent="crypto_seed_loss_lockout_block",
                rationale="loss-streak lockout blocked new directional entry",
                outcome=Outcome.BLOCKED,
                signal=signal.type.value,
            )
            return None

        _is_entry = signal.type in {SignalType.LONG, SignalType.SHORT}
        cap: float | None = None
        if _is_entry:
            sage_bars = self.recent_sage_bars()
            allowed, cap, code = self._ask_jarvis(
                ActionType.ORDER_PLACE,
                rationale=f"{signal.type.value} directional overlay",
                side=signal.type.value,
                symbol=signal.symbol,
                price=signal.price,
                confidence=signal.confidence,
                sage_bars=sage_bars,
                entry_price=signal.price,
            )
            if not allowed:
                self._record_event(
                    intent="crypto_seed_order_blocked",
                    rationale=f"jarvis refused ORDER_PLACE: {code}",
                    outcome=Outcome.BLOCKED,
                    signal=signal.type.value,
                    price=signal.price,
                )
                return None

        if self._router is None:
            self._record_event(
                intent="crypto_seed_paper_sim",
                rationale="no router -- log-only mode",
                outcome=Outcome.NOTED,
                signal=signal.type.value,
                price=signal.price,
            )
            return None
        # Seed-tier uses a fixed 1% notional of equity per directional trade — stop
        # sizing is managed by the 1.5R exit in evaluate_exit rather than a stop_distance.
        notional = self.state.equity * (self.config.risk_per_trade_pct / 100.0)
        if signal.price <= 0.0 or notional <= 0.0:
            return None
        qty = round(notional / signal.price, 6)
        if qty <= 0.0:
            return None
        size_mult = float(signal.meta.get("size_mult", 1.0) or 1.0)
        temporal_mult = float(signal.meta.get("temporal_size_mult", self._last_temporal_size_mult) or 1.0)
        book_mult = max(0.85, min(1.15, 0.85 + self._last_order_book_quality * 0.015))
        freshness_mult = max(0.92, min(1.08, 0.92 + self._last_order_book_freshness_score * 0.012))
        if size_mult > 0.0:
            qty = round(qty * size_mult, 6)
        if temporal_mult > 0.0:
            qty = round(qty * temporal_mult, 6)
        qty = round(qty * book_mult * freshness_mult, 6)
        side = Side.BUY if signal.type is SignalType.LONG else Side.SELL
        qty = round(qty * self._inventory_signal_mult(signal), 6)
        # Apply JARVIS CONDITIONAL cap after all other size multipliers.
        if _is_entry and cap is not None and cap < 1.0:
            qty = round(qty * cap, 6)
        if qty <= 0.0:
            self._record_event(
                intent="crypto_seed_order_zero_qty",
                rationale="composite size multipliers collapsed to zero",
                outcome=Outcome.NOTED,
                signal=signal.type.value,
                cap=cap,
            )
            return None
        req = OrderRequest(symbol=self._venue_symbol, side=side, qty=qty)
        try:
            result = await self._router.place_with_failover(req)
        except Exception as e:  # noqa: BLE001
            logger.error("Seed route failed: %s", e)
            self._record_event(
                intent="crypto_seed_order_route_error",
                rationale=str(e),
                outcome=Outcome.FAILED,
                signal=signal.type.value,
                qty=qty,
            )
            return None
        if result.status is OrderStatus.REJECTED:
            logger.warning("Seed order rejected: id=%s", result.order_id)
            self._record_event(
                intent="crypto_seed_order_rejected",
                rationale="venue rejected order",
                outcome=Outcome.FAILED,
                order_id=result.order_id,
                signal=signal.type.value,
                qty=qty,
            )
        else:
            self._record_event(
                intent="crypto_seed_order_routed",
                rationale="order accepted by venue",
                outcome=Outcome.EXECUTED,
                order_id=result.order_id,
                signal=signal.type.value,
                qty=qty,
                cap=cap,
            )
        return result

    def record_fill(
        self,
        fill: Fill,
        *,
        order_id: str | None = None,
        side: Side | str | None = None,
    ) -> bool:
        """Mark a grid level inactive after a confirmed venue fill."""
        if fill.symbol not in (self.config.symbol, self._venue_symbol):
            return False
        self.update_state(fill)
        delta = float(fill.realized_pnl) - float(fill.fee)
        if abs(float(fill.realized_pnl)) > 1e-12 or float(fill.risk_at_entry) > 0.0:
            if delta < 0.0:
                self._loss_streak += 1
                self._enter_loss_lockout()
            elif delta > 0.0:
                self._loss_streak = 0
            self._refresh_runtime_throttle({})
        fill_side = self._coerce_side(side) or self._coerce_side(fill.side)
        matched = False
        for order in self.grid_state.active_orders:
            if not order.is_active:
                continue
            if order_id and order.order_id and order.order_id == order_id:
                order.is_active = False
                order.status_hint = "FILLED"
                matched = True
                matched_side = self._coerce_side(order.side)
            elif (  # noqa: E501 -- inline triple predicate kept on one `elif`
                fill_side is not None
                and self._coerce_side(order.side) == fill_side
                and abs(float(order.price) - float(fill.price)) <= 1e-9
            ):
                order.is_active = False
                order.status_hint = "FILLED"
                matched = True
                matched_side = fill_side
            if matched:
                if matched_side == Side.BUY:
                    self.grid_state.filled_buys += 1
                elif matched_side == Side.SELL:
                    self.grid_state.filled_sells += 1
                break
        return matched

    # ── Decision Logic ──

    def evaluate_entry(self, bar: dict[str, Any], confluence_score: float) -> bool:
        return confluence_score > 7.0 and self.check_risk() and not self._risk_lockout_active()

    def evaluate_exit(self, position: Position) -> bool:
        risk_usd = self.config.risk_per_trade_pct / 100 * self.state.equity
        if position.unrealized_pnl <= -risk_usd:
            return True
        return position.unrealized_pnl >= 1.5 * risk_usd
