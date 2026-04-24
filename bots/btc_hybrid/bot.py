"""L2 BTC Hybrid Bot -- grid-in-range + directional-on-trend.

Two modes, one market. A regime classifier decides which mode is active
on each bar:

* ``GRID``  -- ranging regime (ADX < trend threshold). Bot places
  symmetric limit orders around the range midpoint, harvesting
  mean-reversion bounces. Inventory is capped so a one-sided breakout
  cannot run the book to zero.
* ``DIRECTIONAL`` -- trending regime (ADX >= trend threshold). Bot
  suspends grid rebalancing and follows EMA-stack direction with a
  single-leg entry sized by ``risk_per_trade_pct``.

Every risk-adding action (ORDER_PLACE, STRATEGY_DEPLOY, CAPITAL_ALLOCATE)
is gated through ``JarvisAdmin.request_approval`` BEFORE being routed
to the venue. If Jarvis replies DENIED / DEFERRED the bot treats the
action as refused, logs the reason code, and moves on. A CONDITIONAL
verdict caps the size at ``response.size_cap_mult`` before routing.

This module keeps the venue router injectable (same contract as ETH
perp), so wiring Bybit or Coinbase is one DI step away. Grid-order
lifecycle now has two paths:

* bar-sweep reconciliation for the current candle-based loop, and
* an explicit fill hook for venue callbacks that know the order id.

That lets the bot stay usable in paper and replay mode while still
being ready for a live fill stream without reshaping the public API.
"""
from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol  # noqa: F401  Any used via noqa

from apex_predator.bots.base_bot import (
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
from apex_predator.bots.crypto_seed.bot import GridOrder, GridState
from apex_predator.brain.jarvis_admin import (
    ActionType,
    JarvisAdmin,
    SubsystemId,
    Verdict,
    make_action_request,
)
from apex_predator.core.market_quality import (
    build_market_context_summary,
    derive_order_book_metrics,
    format_market_context_summary,
)
from apex_predator.obs.decision_journal import Actor, DecisionJournal, Outcome
from apex_predator.strategies.apex_policy import StrategyContext, ob_breaker_retest
from apex_predator.strategies.models import Bar as StrategyBar
from apex_predator.strategies.models import Side as StrategySide
from apex_predator.strategies.models import StrategySignal
from apex_predator.venues.base import (
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from apex_predator.brain.jarvis_context import JarvisContext


logger = logging.getLogger(__name__)


# BTC hybrid bot constants -- finalized from the BTC tape comparison.
#
# The BTC cache shows a strong but not one-dimensional tape: named
# strategy comparison prefers the confluence-heavy BTC primitives, while
# the hybrid still benefits from a real grid regime in calmer pockets.
# The final default profile therefore widens the grid band slightly and
# raises the directional confluence floor so the bot leans into better
# bars instead of churning every transition candle.
_ADX_TREND_BTC: float = 30.0         # >= this -> DIRECTIONAL
_ADX_RANGING_BTC: float = 23.0       # <= this -> GRID (calmer tape)
# Grid geometry
_GRID_LEVELS: int = 6                # 3 buy + 3 sell around mid
_GRID_SPACING_PCT: float = 0.004     # 0.4% between levels (BTC-like)
_GRID_INVENTORY_CAP_PCT: float = 0.25  # max 25% of equity per side
# Confluence floor for DIRECTIONAL entries.
_DIR_MIN_CONFLUENCE: float = 8.0


@dataclass(frozen=True, slots=True)
class BtcHybridProfile:
    """Tunable defaults for the BTC hybrid final edition.

    The live bot still runs as a single L2 state machine. This profile
    just keeps the regime split and sizing geometry explicit so the
    optimization / final-revision script can compare and hand off a
    concrete tuned configuration.
    """

    adx_trending_threshold: float = _ADX_TREND_BTC
    adx_ranging_threshold: float = _ADX_RANGING_BTC
    mode_hysteresis_adx: float = 1.5
    loss_streak_throttle_step: float = 0.15
    loss_streak_throttle_floor: float = 0.5
    loss_streak_lockout_threshold: int = 3
    loss_streak_lockout_bars: int = 6
    volatility_throttle_start: float = 1.6
    volatility_throttle_floor: float = 0.6
    grid_reanchor_drift_pct: float = 0.015
    grid_stale_bars: int = 24
    directional_cooldown_bars: int = 2
    inventory_imbalance_step: float = 0.10
    inventory_imbalance_floor: float = 0.55
    execution_quality_floor: float = 0.40
    directional_quality_floor: float = 0.50
    order_book_quality_floor: float = 4.25
    quality_size_floor: float = 0.55
    session_phase_edge_bias: dict[str, float] = field(default_factory=dict)
    timeframe_edge_bias: dict[str, float] = field(default_factory=dict)
    session_timeframe_edge_bias: dict[str, float] = field(default_factory=dict)
    spread_regime_edge_bias: dict[str, float] = field(default_factory=dict)
    order_book_quality_edge_bias: dict[str, float] = field(default_factory=dict)
    session_phase_size_bias: dict[str, float] = field(default_factory=dict)
    timeframe_size_bias: dict[str, float] = field(default_factory=dict)
    session_timeframe_size_bias: dict[str, float] = field(default_factory=dict)
    spread_regime_size_bias: dict[str, float] = field(default_factory=dict)
    order_book_quality_size_bias: dict[str, float] = field(default_factory=dict)
    grid_levels: int = _GRID_LEVELS
    grid_spacing_pct: float = _GRID_SPACING_PCT
    grid_inventory_cap_pct: float = _GRID_INVENTORY_CAP_PCT
    dir_min_confluence: float = _DIR_MIN_CONFLUENCE

    def __post_init__(self) -> None:
        if self.grid_levels < 2 or self.grid_levels % 2 != 0:
            raise ValueError("grid_levels must be an even integer >= 2")
        if self.adx_ranging_threshold >= self.adx_trending_threshold:
            raise ValueError("adx_ranging_threshold must be < adx_trending_threshold")
        if self.mode_hysteresis_adx < 0.0:
            raise ValueError("mode_hysteresis_adx must be non-negative")
        if self.loss_streak_throttle_step < 0.0:
            raise ValueError("loss_streak_throttle_step must be non-negative")
        if not (0.0 < self.loss_streak_throttle_floor <= 1.0):
            raise ValueError("loss_streak_throttle_floor must be in (0, 1]")
        if self.loss_streak_lockout_threshold < 1:
            raise ValueError("loss_streak_lockout_threshold must be >= 1")
        if self.loss_streak_lockout_bars < 1:
            raise ValueError("loss_streak_lockout_bars must be >= 1")
        if self.volatility_throttle_start <= 0.0:
            raise ValueError("volatility_throttle_start must be positive")
        if not (0.0 < self.volatility_throttle_floor <= 1.0):
            raise ValueError("volatility_throttle_floor must be in (0, 1]")
        if self.grid_reanchor_drift_pct <= 0.0:
            raise ValueError("grid_reanchor_drift_pct must be positive")
        if self.grid_stale_bars < 1:
            raise ValueError("grid_stale_bars must be >= 1")
        if self.directional_cooldown_bars < 0:
            raise ValueError("directional_cooldown_bars must be >= 0")
        if self.inventory_imbalance_step < 0.0:
            raise ValueError("inventory_imbalance_step must be non-negative")
        if not (0.0 < self.inventory_imbalance_floor <= 1.0):
            raise ValueError("inventory_imbalance_floor must be in (0, 1]")
        if not (0.0 <= self.execution_quality_floor <= 1.0):
            raise ValueError("execution_quality_floor must be in [0, 1]")
        if not (0.0 <= self.directional_quality_floor <= 1.0):
            raise ValueError("directional_quality_floor must be in [0, 1]")
        if not (0.0 <= self.order_book_quality_floor <= 10.0):
            raise ValueError("order_book_quality_floor must be in [0, 10]")
        if not (0.0 < self.quality_size_floor <= 1.0):
            raise ValueError("quality_size_floor must be in (0, 1]")
        for name, biases in (
            ("session_phase_edge_bias", self.session_phase_edge_bias),
            ("timeframe_edge_bias", self.timeframe_edge_bias),
            ("session_timeframe_edge_bias", self.session_timeframe_edge_bias),
            ("spread_regime_edge_bias", self.spread_regime_edge_bias),
            ("order_book_quality_edge_bias", self.order_book_quality_edge_bias),
            ("session_phase_size_bias", self.session_phase_size_bias),
            ("timeframe_size_bias", self.timeframe_size_bias),
            ("session_timeframe_size_bias", self.session_timeframe_size_bias),
            ("spread_regime_size_bias", self.spread_regime_size_bias),
            ("order_book_quality_size_bias", self.order_book_quality_size_bias),
        ):
            for key, value in biases.items():
                if not key:
                    raise ValueError(f"{name} keys must be non-empty")
                try:
                    bias = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{name}[{key!r}] must be numeric") from exc
                if not (0.0 < bias <= 2.0):
                    raise ValueError(f"{name}[{key!r}] must be in (0, 2]")
        if self.grid_spacing_pct <= 0.0:
            raise ValueError("grid_spacing_pct must be positive")
        if not (0.0 < self.grid_inventory_cap_pct <= 1.0):
            raise ValueError("grid_inventory_cap_pct must be in (0, 1]")
        if self.dir_min_confluence <= 0.0:
            raise ValueError("dir_min_confluence must be positive")


BTC_HYBRID_CONFIG = BotConfig(
    name="BTC-Hybrid",
    symbol="BTCUSDT",
    tier=Tier.CASINO,
    baseline_usd=5000.0,
    starting_capital_usd=5000.0,
    max_leverage=3.0,              # L2 is spot-ish; directional leg caps low
    risk_per_trade_pct=1.5,        # directional leg -- grid sizes separately
    daily_loss_cap_pct=4.0,
    max_dd_kill_pct=12.0,
    margin_mode=MarginMode.CROSS,
)


class HybridMode(StrEnum):
    """Which strategy mode the bot is currently running."""
    GRID = "GRID"
    DIRECTIONAL = "DIRECTIONAL"
    FLAT = "FLAT"            # transition / stood-down


class _Router(Protocol):
    async def place_with_failover(self, req: OrderRequest) -> OrderResult: ...


class BtcHybridBot(BaseBot):
    """L2 BTC grid-in-range + directional-on-trend bot, JARVIS-gated."""

    SUBSYSTEM: SubsystemId = SubsystemId.BOT_BTC_HYBRID

    def __init__(
        self,
        config: BotConfig | None = None,
        *,
        jarvis: JarvisAdmin,
        router: _Router | None = None,
        venue_symbol: str | None = None,
        profile: BtcHybridProfile | None = None,
        provide_ctx: Callable[[], JarvisContext] | None = None,
        journal: DecisionJournal | None = None,
    ) -> None:
        """Construct an L2 BTC hybrid bot.

        Parameters
        ----------
        config:
            Optional override of ``BTC_HYBRID_CONFIG``.
        jarvis:
            The ``JarvisAdmin`` instance gating every risk-adding
            action. Required -- this bot intentionally has no
            Jarvis-bypass path.
        router:
            Optional venue router. When ``None`` the bot runs in
            paper-sim mode: JARVIS verdicts are still logged, but no
            orders are placed.
        venue_symbol:
            Exchange-specific contract symbol. Defaults to
            ``config.symbol``.
        provide_ctx:
            Callable returning the current ``JarvisContext``. When
            supplied, each Jarvis request uses this context. When
            ``None`` the caller MUST have wired an engine into the
            ``JarvisAdmin`` so it can self-tick.
        """
        super().__init__(config or BTC_HYBRID_CONFIG)
        self._jarvis = jarvis
        self._router = router
        self._venue_symbol = venue_symbol or self.config.symbol
        self.profile = profile or BtcHybridProfile()
        self._provide_ctx = provide_ctx
        self._journal = journal
        self._mode: HybridMode = HybridMode.FLAT
        self._grid_bounds_high = 0.0
        self._grid_bounds_low = 0.0
        self._grid_anchor_mid = 0.0
        self._active_grid_spacing_pct = self.profile.grid_spacing_pct
        self._active_grid_inventory_cap_pct = self.profile.grid_inventory_cap_pct
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
        self._last_temporal_size_mult = 1.0
        self._last_session_size_bias = 1.0
        self._last_timeframe_size_bias = 1.0
        self._last_session_timeframe_size_bias = 1.0
        self._last_spread_size_bias = 1.0
        self._market_quality_blocked = False
        self._last_session_phase = "UNKNOWN"
        self._last_timeframe_label = "UNKNOWN"
        self._last_session_timeframe_key = "UNKNOWN::UNKNOWN"
        self._last_timeframe_minutes = 0.0
        self._last_microstructure_score = 5.0
        self._last_pattern_edge_score = 5.0
        self._recent_bars: deque[StrategyBar] = deque(maxlen=128)
        self._current_bar_idx = 0
        self._loss_streak = 0
        self._throttle_mult = 1.0
        self._volatility_throttle_mult = 1.0
        self._last_directional_bar_idx = -10_000
        self._risk_lockout_until_bar_idx = -10_000
        self.grid_state = GridState()
        # Grid bookkeeping -- placeholders for the lifecycle wiring.
        # ``_grid_levels[i]`` maps the arming price to an open order id
        # so a fill callback knows which level to re-arm. This is kept
        # in-memory-only for now; persistence happens in a later batch
        # when the obs journal integrates.
        self._grid_levels: dict[float, str] = {}
        self._grid_level_side: dict[float, Side] = {}
        self._grid_level_status: dict[float, str] = {}
        self._grid_level_armed_idx: dict[float, int] = {}
        self._grid_side_counts: dict[Side, int] = {
            Side.BUY: 0,
            Side.SELL: 0,
        }

    # ------------------------------------------------------------------
    # JARVIS gating helper (the contribution this bot makes over L3 bots)
    # ------------------------------------------------------------------

    def _ask_jarvis(
        self,
        action: ActionType,
        **payload: Any,  # noqa: ANN401 -- deliberately untyped by design
    ) -> tuple[bool, float | None, str]:
        """Gate a risk-adding action through JARVIS.

        Returns ``(allowed, size_cap_mult, reason_code)`` where:
          * ``allowed`` -- True if the action may proceed (APPROVED or
            CONDITIONAL). False for DENIED / DEFERRED.
          * ``size_cap_mult`` -- Optional multiplier from CONDITIONAL;
            None under APPROVED when no cap is set.
          * ``reason_code`` -- Machine-readable code from Jarvis.
        """
        req = make_action_request(
            subsystem=self.SUBSYSTEM,
            action=action,
            rationale=payload.pop("rationale", ""),
            **payload,
        )
        ctx = self._provide_ctx() if self._provide_ctx else None
        resp = self._jarvis.request_approval(req, ctx=ctx)
        allowed = resp.verdict in (Verdict.APPROVED, Verdict.CONDITIONAL)
        if not allowed:
            logger.info(
                "%s jarvis refused %s: %s (%s)",
                self.config.name, action.value,
                resp.reason, resp.reason_code,
            )
        elif resp.verdict == Verdict.CONDITIONAL:
            logger.info(
                "%s jarvis conditional %s: size_cap=%.3f (%s)",
                self.config.name, action.value,
                resp.size_cap_mult or 1.0, resp.reason_code,
            )
        return allowed, resp.size_cap_mult, resp.reason_code

    def pick_model_tier(
        self,
        category: Any,  # noqa: ANN401 -- TaskCategory, local import to avoid cycle
        *,
        rationale: str = "",
    ) -> Any:  # noqa: ANN401 -- ModelTier
        """Ask JARVIS which model tier to use for a given task.

        Bot-side convenience for operator-level retros / explanations
        routed through JARVIS. Uses
        :meth:`JarvisAdmin.select_llm_tier` under the hood so the
        audit log records the decision exactly like an LLM_INVOCATION
        request. Returns the :class:`ModelTier` JARVIS chose.
        """
        from apex_predator.brain.jarvis_gate import pick_llm_tier
        return pick_llm_tier(
            self._jarvis,
            subsystem=self.SUBSYSTEM,
            category=category,
            rationale=rationale,
        )

    def _record_event(
        self,
        *,
        intent: str,
        rationale: str = "",
        outcome: Outcome = Outcome.NOTED,
        **metadata: Any,  # noqa: ANN401 -- journal payloads are intentionally flexible
    ) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record(
                actor=Actor.TRADE_ENGINE,
                intent=intent,
                rationale=rationale,
                outcome=outcome,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001 - logging only
            logger.warning("%s journal write failed: %s", self.config.name, exc)

    @staticmethod
    def _as_float(raw: Any, default: float = 0.0) -> float:  # noqa: ANN401 -- raw bar payloads are deliberately untyped
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_int(raw: Any, default: int = 0) -> int:  # noqa: ANN401 -- raw bar payloads are deliberately untyped
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clamp01(value: float) -> float:
        return max(0.0, min(1.0, value))

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

    def _refresh_tape_quality(self, bar: dict[str, Any]) -> None:
        bar = self._bar_with_fallbacks(bar)
        close = self._as_float(bar.get("close"), 0.0)
        open_ = self._as_float(bar.get("open"), close)
        high = self._as_float(bar.get("high"), close)
        low = self._as_float(bar.get("low"), close)
        body = abs(close - open_)
        candle_range = max(high - low, 0.0)
        body_efficiency = self._clamp01(body / candle_range) if candle_range > 0.0 else 0.0

        volumes = self._recent_volume_values()
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
        ema_gap_score = self._clamp01(ema_gap_pct / max(self.profile.grid_spacing_pct * 2.5, 1e-6))

        adx = self._as_float(bar.get("adx_14"), 0.0)
        adx_span = max(self.profile.adx_trending_threshold - self.profile.adx_ranging_threshold, 1e-9)
        adx_score = self._clamp01((adx - self.profile.adx_ranging_threshold) / adx_span)

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
        elif directional_quality >= 0.60 and adx >= self.profile.adx_trending_threshold:
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
        self._last_order_book_venue = str(  # noqa: E501 -- one-line fallback chain for venue id
            bar.get("order_book_venue") or bar.get("venue") or self._last_order_book_venue or "",
        )
        try:
            self._last_order_book_depth = int(float(  # noqa: E501 -- one-line fallback chain for depth
                bar.get("order_book_depth") or self._last_order_book_depth or 0,
            ))
        except (TypeError, ValueError):
            self._last_order_book_depth = int(self._last_order_book_depth or 0)
        self._last_order_book_age_ms = float(  # noqa: E501 -- one-line fallback chain for age
            bar.get("order_book_age_ms") or order_book_metrics["order_book_age_ms"] or 0.0,
        )
        self._last_order_book_depth_score = order_book_depth_score
        self._last_order_book_freshness_score = order_book_freshness_score
        self._last_order_book_quality = order_book_quality
        self._refresh_temporal_state(bar)
        self._market_quality_blocked = (
            market_quality < 0.30
            or (
                execution_quality < self.profile.execution_quality_floor
                and directional_quality < self.profile.directional_quality_floor
            )
            or order_book_quality < self.profile.order_book_quality_floor
        )

    def _quality_signal_mult(self, score: float) -> float:
        return max(
            self.profile.quality_size_floor,
            min(1.5, self.profile.quality_size_floor + score * (1.0 - self.profile.quality_size_floor)),
        )

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
        session_bias = self._temporal_bias(
            self.profile.session_phase_size_bias,
            session_phase or self._last_session_phase,
        )
        timeframe_bias = self._temporal_bias(
            self.profile.timeframe_size_bias,
            timeframe_label or self._last_timeframe_label,
        )
        spread_bias = self._temporal_bias(
            self.profile.spread_regime_size_bias,
            spread_regime or self._market_quality_spread_regime,
        )
        session_timeframe_bias = self._temporal_bias(
            self.profile.session_timeframe_size_bias,
            self._session_timeframe_key(  # noqa: E501 -- key builder call is inline for clarity
                session_phase or self._last_session_phase,
                timeframe_label or self._last_timeframe_label,
            ),
        )
        micro = max(
            0.92,
            min(
                1.08,
                0.92
                + max(
                    0.0,
                    min(  # noqa: E501 -- microstructure clamp keeps formula on one line
                        10.0,
                        microstructure_score
                        if microstructure_score is not None
                        else self._last_microstructure_score,
                    ),
                ) * 0.016,
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
                ) * 0.016,
            ),
        )
        quality_bucket_bias = self._temporal_bias(
            self.profile.order_book_quality_size_bias,
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
                ) * 0.012,
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
                ) * 0.020,
            ),
        )
        combined = (
            session_bias * timeframe_bias * session_timeframe_bias * spread_bias
            * micro * book_quality * freshness * edge * quality_bucket_bias
        )
        return max(0.65, min(1.35, combined))

    def _refresh_temporal_state(self, bar: dict[str, Any]) -> None:
        session_phase = str(bar.get("session_phase", self._last_session_phase)).strip().upper() or "UNKNOWN"
        timeframe_minutes = self._as_float(bar.get("timeframe_minutes"), self._last_timeframe_minutes)
        timeframe_label = str(bar.get("timeframe_label", self._timeframe_label(timeframe_minutes))).strip().upper()
        if timeframe_label == "UNKNOWN":
            timeframe_label = self._timeframe_label(timeframe_minutes)
        session_timeframe_key = self._session_timeframe_key(session_phase, timeframe_label)
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
        self._last_session_size_bias = self._temporal_bias(
            self.profile.session_phase_size_bias, session_phase,
        )
        self._last_timeframe_size_bias = self._temporal_bias(
            self.profile.timeframe_size_bias, timeframe_label,
        )
        self._last_session_timeframe_size_bias = self._temporal_bias(
            self.profile.session_timeframe_size_bias, session_timeframe_key,
        )
        self._last_spread_size_bias = self._temporal_bias(
            self.profile.spread_regime_size_bias, spread_regime,
        )
        self._last_order_book_quality_bucket = order_book_quality_bucket
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

    def _quality_block_reason(self) -> str:
        reasons: list[str] = []
        if self._execution_quality < self.profile.execution_quality_floor:
            reasons.append(
                f"execution={self._execution_quality:.2f}<floor={self.profile.execution_quality_floor:.2f}",
            )
        if self._directional_quality < self.profile.directional_quality_floor:
            reasons.append(
                f"directional={self._directional_quality:.2f}<floor={self.profile.directional_quality_floor:.2f}",
            )
        if self._market_quality_label == "THIN":
            reasons.append("market=THIN")
        if self._last_order_book_quality < self.profile.order_book_quality_floor:
            reasons.append(
                f"order_book={self._last_order_book_quality:.2f}<floor={self.profile.order_book_quality_floor:.2f}",
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

    def seed_history(self, bars: Iterable[dict[str, Any]]) -> None:
        """Seed the directional strategy history from warmup bars."""
        self._recent_bars.clear()
        self._current_bar_idx = 0
        for bar in bars:
            self._append_recent_bar(bar)
            self._current_bar_idx = self._as_int(
                bar.get("bar_idx"),
                self._current_bar_idx + 1,
            )

    def _bar_index_for(self, bar: dict[str, Any]) -> int:
        raw_idx = bar.get("bar_idx")
        if raw_idx is None:
            self._current_bar_idx += 1
            return self._current_bar_idx
        idx = self._as_int(raw_idx, self._current_bar_idx + 1)
        self._current_bar_idx = idx
        return idx

    def _refresh_runtime_throttle(self, bar: dict[str, Any]) -> None:
        bar = self._bar_with_fallbacks(bar)
        atr_14 = self._as_float(bar.get("atr_14"), 0.0)
        avg_atr_50 = self._as_float(bar.get("avg_atr_50"), 0.0)
        vol_mult = 1.0
        if atr_14 > 0.0 and avg_atr_50 > 0.0:
            ratio = atr_14 / avg_atr_50
            if ratio > self.profile.volatility_throttle_start:
                stretch = min(
                    1.0,
                    (ratio - self.profile.volatility_throttle_start)
                    / max(self.profile.volatility_throttle_start, 1e-9),
                )
                vol_mult = 1.0 - stretch * (1.0 - self.profile.volatility_throttle_floor)
        loss_mult = 1.0 - (self._loss_streak * self.profile.loss_streak_throttle_step)
        self._volatility_throttle_mult = max(
            self.profile.volatility_throttle_floor,
            min(1.0, vol_mult),
        )
        self._throttle_mult = max(
            0.0,
            min(
                1.0,
                self._volatility_throttle_mult,
                max(self.profile.loss_streak_throttle_floor, loss_mult),
            ),
        )

    @property
    def runtime_snapshot(self) -> dict[str, Any]:
        """Expose the current BTC runtime state to the live supervisor."""
        lockout_active = self._risk_lockout_active()
        snapshot = {
            "mode": self._mode.value,
            "current_bar_idx": self._current_bar_idx,
            "loss_streak": self._loss_streak,
            "throttle_mult": round(self._throttle_mult, 4),
            "volatility_throttle_mult": round(self._volatility_throttle_mult, 4),
            "grid_anchor_mid": round(self._grid_anchor_mid, 6),
            "active_grid_spacing_pct": round(self._active_grid_spacing_pct, 6),
            "active_grid_inventory_cap_pct": round(self._active_grid_inventory_cap_pct, 6),
            "armed_grid_count": len(self._grid_levels),
            "grid_buy_armed": self._grid_side_counts[Side.BUY],
            "grid_sell_armed": self._grid_side_counts[Side.SELL],
            "grid_filled_buys": self.grid_state.filled_buys,
            "grid_filled_sells": self.grid_state.filled_sells,
            "grid_fill_imbalance": self._inventory_fill_imbalance(),
            "risk_lockout_active": lockout_active,
            "risk_lockout_until_bar_idx": self._risk_lockout_until_bar_idx if lockout_active else None,
            "risk_lockout_remaining_bars": max(
                0,
                self._risk_lockout_until_bar_idx - self._current_bar_idx,
            ) if lockout_active else 0,
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
            "market_quality_blocked": self._market_quality_blocked,
            "session_phase": self._last_session_phase,
            "timeframe_label": self._last_timeframe_label,
            "session_timeframe_key": self._last_session_timeframe_key,
            "timeframe_minutes": round(self._last_timeframe_minutes, 4),
            "microstructure_score": round(self._last_microstructure_score, 4),
            "pattern_edge_score": round(self._last_pattern_edge_score, 4),
            "recent_bar_count": len(self._recent_bars),
            "directional_cooldown_bars": self.profile.directional_cooldown_bars,
            "directional_cooldown_remaining": max(
                0,
                self.profile.directional_cooldown_bars
                - max(0, self._current_bar_idx - self._last_directional_bar_idx),
            ),
        }
        market_context_summary = build_market_context_summary(snapshot)
        if market_context_summary:
            snapshot["market_context_summary"] = market_context_summary
            snapshot["market_context_summary_text"] = format_market_context_summary(market_context_summary)
        return snapshot

    def _recent_close_values(self, lookback: int = 50) -> list[float]:
        return [
            float(bar.close)
            for bar in list(self._recent_bars)[-lookback:]
            if float(bar.close) > 0.0
        ]

    def _recent_volume_values(self, lookback: int = 50) -> list[float]:
        return [
            float(bar.volume)
            for bar in list(self._recent_bars)[-lookback:]
            if float(bar.volume) >= 0.0
        ]

    @staticmethod
    def _ema_from_values(values: list[float], span: int) -> float:
        if not values:
            return 0.0
        alpha = 2.0 / (float(span) + 1.0)
        ema = float(values[0])
        for value in values[1:]:
            ema = alpha * float(value) + (1.0 - alpha) * ema
        return ema

    @staticmethod
    def _derived_adx_from_closes(values: list[float]) -> float:
        if len(values) < 3:
            return 0.0
        total_move = sum(abs(values[idx] - values[idx - 1]) for idx in range(1, len(values)))
        if total_move <= 0.0:
            return 0.0
        net_move = abs(values[-1] - values[0])
        return max(0.0, min(50.0, 100.0 * net_move / total_move))

    def _bar_with_fallbacks(self, bar: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(bar)
        recent_bars = list(self._recent_bars)
        closes = self._recent_close_values()
        volumes = self._recent_volume_values()
        if closes:
            close_tail_9 = closes[-9:]
            close_tail_21 = closes[-21:]
            if self._as_float(enriched.get("ema_9"), 0.0) <= 0.0 and len(close_tail_9) >= 2:
                enriched["ema_9"] = self._ema_from_values(close_tail_9, 9)
            if self._as_float(enriched.get("ema_21"), 0.0) <= 0.0 and len(close_tail_21) >= 2:
                enriched["ema_21"] = self._ema_from_values(close_tail_21, 21)
            if self._as_float(enriched.get("adx_14"), 0.0) <= 0.0:
                enriched["adx_14"] = self._derived_adx_from_closes(closes[-14:])
            if self._as_float(enriched.get("confluence_score"), 0.0) <= 0.0:
                ema_9 = self._as_float(enriched.get("ema_9"), 0.0)
                ema_21 = self._as_float(enriched.get("ema_21"), 0.0)
                adx = self._as_float(enriched.get("adx_14"), 0.0)
                trend_gap = 0.0
                if ema_9 > 0.0 and ema_21 > 0.0:
                    trend_gap = abs(ema_9 - ema_21) / max(ema_21, 1.0) * 100.0
                latest_volume = self._as_float(
                    enriched.get("volume"),
                    volumes[-1] if volumes else 0.0,
                )
                avg_volume = self._as_float(enriched.get("avg_volume"), 0.0)
                if avg_volume <= 0.0 and volumes:
                    avg_volume = statistics.fmean(volumes[-20:])
                    enriched["avg_volume"] = avg_volume
                vol_ratio = latest_volume / avg_volume if avg_volume > 0.0 else 0.0
                confluence = 5.0 + adx * 0.12 + min(3.0, trend_gap * 0.5) + max(0.0, vol_ratio - 1.0)
                enriched["confluence_score"] = round(min(10.0, confluence), 3)
        if self._as_float(enriched.get("avg_volume"), 0.0) <= 0.0 and volumes:
            enriched["avg_volume"] = statistics.fmean(volumes[-20:])
        if self._as_float(enriched.get("atr_14"), 0.0) <= 0.0 and recent_bars:
            ranges = [
                max(float(bar.high) - float(bar.low), 0.0)
                for bar in recent_bars[-14:]
            ]
            ranges = [value for value in ranges if value > 0.0]
            if ranges:
                enriched["atr_14"] = statistics.fmean(ranges)
        if self._as_float(enriched.get("avg_atr_50"), 0.0) <= 0.0 and recent_bars:
            ranges = [
                max(float(bar.high) - float(bar.low), 0.0)
                for bar in recent_bars[-50:]
            ]
            ranges = [value for value in ranges if value > 0.0]
            if ranges:
                enriched["avg_atr_50"] = statistics.fmean(ranges)
        if (
            self._as_float(enriched.get("bb_upper"), 0.0) <= 0.0
            or self._as_float(enriched.get("bb_lower"), 0.0) <= 0.0
        ) and len(closes) >= 5:
            tail = closes[-20:]
            center = statistics.fmean(tail)
            spread = statistics.pstdev(tail) if len(tail) > 1 else 0.0
            band_width = max(center * 0.0015, spread * 2.0)
            if self._as_float(enriched.get("bb_upper"), 0.0) <= 0.0:
                enriched["bb_upper"] = center + band_width
            if self._as_float(enriched.get("bb_lower"), 0.0) <= 0.0:
                enriched["bb_lower"] = center - band_width
        return enriched

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

    @staticmethod
    def _temporal_bias(weight_map: dict[str, float], key: str) -> float:
        if not key or key == "UNKNOWN":
            return 1.0
        try:
            value = float(weight_map.get(key, 1.0))
        except (TypeError, ValueError):
            value = 1.0
        return max(0.5, min(1.5, value))

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
        score = 5.0
        score *= self._temporal_bias(self.profile.session_phase_edge_bias, session_phase)
        score *= self._temporal_bias(self.profile.timeframe_edge_bias, timeframe_label)
        score *= self._temporal_bias(
            self.profile.session_timeframe_edge_bias,
            self._session_timeframe_key(session_phase, timeframe_label),
        )
        regime_label = self._spread_regime_label(spread_bps, book_imbalance, spread_regime)
        score *= self._temporal_bias(self.profile.spread_regime_edge_bias, regime_label)
        score *= self._temporal_bias(self.profile.order_book_quality_edge_bias, order_book_quality_bucket)
        score *= {
            "TIGHT": 1.06,
            "NORMAL": 1.00,
            "WIDE": 0.96,
            "STRESSED": 0.90,
        }.get(regime_label, 1.0)
        score *= max(0.90, min(1.10, 1.0 - abs(book_imbalance) * 0.2))
        score *= max(0.85, min(1.15, 0.85 + max(0.0, min(10.0, microstructure_score)) * 0.03))
        score *= max(0.90, min(1.10, 0.90 + max(0.0, min(10.0, order_book_depth_score)) * 0.02))
        score *= max(0.92, min(1.08, 0.92 + max(0.0, min(10.0, order_book_freshness_score)) * 0.016))
        score *= max(0.90, min(1.10, 0.90 + max(0.0, min(10.0, order_book_quality)) * 0.018))
        return max(0.0, min(10.0, score))

    def _strategy_context(self, bar: dict[str, Any]) -> StrategyContext:
        bar = self._bar_with_fallbacks(bar)
        ema_9 = self._as_float(bar.get("ema_9"), 0.0)
        ema_21 = self._as_float(bar.get("ema_21"), 0.0)
        atr_14 = self._as_float(bar.get("atr_14"), 0.0)
        avg_atr_50 = self._as_float(bar.get("avg_atr_50"), 0.0)
        quality = self._market_quality
        vol_z = 0.0
        if atr_14 > 0.0 and avg_atr_50 > 0.0:
            vol_z = max(0.0, min(4.0, ((atr_14 / avg_atr_50) - 1.0) * 2.0))
        trend_bias = StrategySide.LONG if ema_9 >= ema_21 else StrategySide.SHORT
        session_phase = str(bar.get("session_phase", "UNKNOWN")).strip().upper() or "UNKNOWN"
        timeframe_minutes = self._as_float(bar.get("timeframe_minutes"), 0.0)
        timeframe_label = str(bar.get("timeframe_label", self._timeframe_label(timeframe_minutes))).strip().upper()
        if timeframe_label == "UNKNOWN":
            timeframe_label = self._timeframe_label(timeframe_minutes)
        session_timeframe_key = self._session_timeframe_key(session_phase, timeframe_label)
        base_pattern_edge_score = self._as_float(bar.get("pattern_edge_score"), 5.0)
        microstructure_score = self._as_float(
            bar.get("microstructure_score"),
            max(0.0, min(10.0, self._market_quality * 10.0)),
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
        learned_pattern_edge_score = self._pattern_edge_score(
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
        pattern_edge_score = max(
            0.0,
            min(10.0, 0.5 * base_pattern_edge_score + 0.5 * learned_pattern_edge_score),
        )
        self._last_session_phase = session_phase
        self._last_timeframe_label = timeframe_label
        self._last_session_timeframe_key = session_timeframe_key
        self._last_timeframe_minutes = timeframe_minutes
        self._last_microstructure_score = microstructure_score
        self._last_pattern_edge_score = pattern_edge_score
        self._last_session_size_bias = self._temporal_bias(
            self.profile.session_phase_size_bias, session_phase,
        )
        self._last_timeframe_size_bias = self._temporal_bias(
            self.profile.timeframe_size_bias, timeframe_label,
        )
        self._last_session_timeframe_size_bias = self._temporal_bias(
            self.profile.session_timeframe_size_bias, session_timeframe_key,
        )
        self._last_spread_size_bias = self._temporal_bias(
            self.profile.spread_regime_size_bias, spread_regime,
        )
        self._last_order_book_quality_bucket = order_book_quality_bucket
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
        return StrategyContext(
            regime_label=self._infer_regime(bar, profile=self.profile).value,
            confluence_score=self._as_float(bar.get("confluence_score"), 5.0),
            vol_z=vol_z,
            trend_bias=trend_bias,
            session_allows_entries=not self.state.is_paused,
            kill_switch_active=self.state.is_killed,
            htf_bias=trend_bias,
            session_phase=session_phase,
            timeframe_label=timeframe_label,
            session_timeframe_key=session_timeframe_key,
            timeframe_minutes=timeframe_minutes,
            microstructure_score=microstructure_score,
            pattern_edge_score=pattern_edge_score,
            spread_bps=spread_bps,
            book_imbalance=book_imbalance,
            spread_regime=spread_regime,
            order_book_age_ms=order_book_metrics["order_book_age_ms"],
            order_book_depth_score=order_book_depth_score,
            order_book_freshness_score=order_book_freshness_score,
            order_book_quality=order_book_quality,
            order_book_quality_bucket=order_book_quality_bucket,
            meta={
                "adx_14": self._as_float(bar.get("adx_14"), 0.0),
                "atr_14": atr_14,
                "avg_atr_50": avg_atr_50,
                "market_quality": quality,
                "execution_quality": self._execution_quality,
                "directional_quality": self._directional_quality,
                "market_quality_label": self._market_quality_label,
                "volume_ratio": self._market_quality_volume_ratio,
                "timeframe_minutes": timeframe_minutes,
                "session_timeframe_key": session_timeframe_key,
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
                "temporal_size_mult": self._last_temporal_size_mult,
                "session_size_bias": self._last_session_size_bias,
                "timeframe_size_bias": self._last_timeframe_size_bias,
                "spread_size_bias": self._last_spread_size_bias,
            },
        )

    def _set_launch_geometry_from_bar(self, bar: dict[str, Any] | None) -> None:
        self._active_grid_spacing_pct = self.profile.grid_spacing_pct
        self._active_grid_inventory_cap_pct = self.profile.grid_inventory_cap_pct
        if bar is None:
            return
        bar = self._bar_with_fallbacks(bar)
        atr_14 = self._as_float(bar.get("atr_14"), 0.0)
        avg_atr_50 = self._as_float(bar.get("avg_atr_50"), 0.0)
        close = self._as_float(bar.get("close"), 0.0)
        spacing_scale = 1.0
        cap_scale = 1.0
        if atr_14 > 0.0 and avg_atr_50 > 0.0:
            vol_ratio = atr_14 / avg_atr_50
            spacing_scale = max(0.8, min(1.45, vol_ratio))
            cap_scale = max(0.65, min(1.10, 1.0 / max(vol_ratio, 1e-9)))
        elif close > 0.0:
            bb_upper = self._as_float(bar.get("bb_upper"), close)
            bb_lower = self._as_float(bar.get("bb_lower"), close)
            bb_width = max(0.0, bb_upper - bb_lower)
            width_pct = bb_width / close if close > 0.0 else 0.0
            if width_pct > 0.0:
                spacing_scale = max(
                    0.8,
                    min(1.35, width_pct / max(self.profile.grid_spacing_pct * self.profile.grid_levels, 1e-6)),
                )
                cap_scale = max(0.7, min(1.10, 1.0 - max(0.0, width_pct - 0.01) * 2.0))
        quality_spacing_scale = max(
            0.85,
            min(1.20, 1.10 - (self._execution_quality - 0.5) * 0.35),
        )
        quality_cap_scale = max(
            0.70,
            min(1.20, 0.85 + self._execution_quality * 0.30),
        )
        spacing_scale *= quality_spacing_scale
        cap_scale *= quality_cap_scale
        self._active_grid_spacing_pct = self.profile.grid_spacing_pct * spacing_scale
        self._active_grid_inventory_cap_pct = max(
            0.05,
            min(1.0, self.profile.grid_inventory_cap_pct * cap_scale),
        )

    def _breaker_retest_signal(self, bar: dict[str, Any]) -> StrategySignal | None:
        if len(self._recent_bars) < 10:
            return None
        if self._directional_quality < self.profile.directional_quality_floor:
            self._record_event(
                intent="btc_hybrid_directional_quality_block",
                rationale=self._quality_block_reason(),
                outcome=Outcome.BLOCKED,
                bar_idx=self._current_bar_idx,
            )
            return None
        signal = ob_breaker_retest(list(self._recent_bars), self._strategy_context(bar))
        if not signal.is_actionable or signal.side is StrategySide.FLAT:
            return None
        quality_boost = max(0.0, self._directional_quality - self.profile.directional_quality_floor)
        signal = StrategySignal(
            side=signal.side,
            entry=signal.entry,
            stop=signal.stop,
            target=signal.target,
            confidence=min(10.0, float(signal.confidence) + quality_boost * 3.0),
            risk_mult=max(
                self.profile.quality_size_floor,
                min(1.5, float(signal.risk_mult or 1.0)),
            ),
            strategy=signal.strategy,
            rationale_tags=signal.rationale_tags,
            meta=dict(signal.meta),
        )
        return signal

    def _inventory_fill_imbalance(self) -> int:
        return self.grid_state.filled_buys - self.grid_state.filled_sells

    def _inventory_side_mult(self, side: Side) -> float:
        imbalance = self._inventory_fill_imbalance()
        if imbalance == 0:
            return 1.0
        if side == Side.BUY and imbalance > 0:
            return max(
                self.profile.inventory_imbalance_floor,
                1.0 - imbalance * self.profile.inventory_imbalance_step,
            )
        if side == Side.SELL and imbalance < 0:
            return max(
                self.profile.inventory_imbalance_floor,
                1.0 - abs(imbalance) * self.profile.inventory_imbalance_step,
            )
        return 1.0

    def _inventory_signal_mult(self, signal: Signal) -> float:
        if signal.type == SignalType.GRID_ADD:
            return self._inventory_side_mult(self._grid_side_for_signal(signal))
        imbalance = abs(self._inventory_fill_imbalance())
        if imbalance <= 0:
            return 1.0
        return max(
            self.profile.inventory_imbalance_floor,
            1.0 - imbalance * (self.profile.inventory_imbalance_step / 2.0),
        )

    def _risk_lockout_active(self) -> bool:
        return self._risk_lockout_until_bar_idx > self._current_bar_idx

    def _enter_loss_lockout(self, *, source: str) -> None:
        if self._loss_streak < self.profile.loss_streak_lockout_threshold:
            return
        until = self._current_bar_idx + self.profile.loss_streak_lockout_bars
        if until <= self._risk_lockout_until_bar_idx:
            return
        previous = self._risk_lockout_until_bar_idx
        self._risk_lockout_until_bar_idx = until
        self._record_event(
            intent="btc_hybrid_loss_lockout_enter",
            rationale=f"loss streak={self._loss_streak} triggered lockout",
            outcome=Outcome.NOTED,
            source=source,
            bar_idx=self._current_bar_idx,
            lockout_until_bar_idx=until,
            previous_until_bar_idx=previous,
        )

    def _refresh_risk_lockout(self) -> None:
        if self._risk_lockout_until_bar_idx <= -10_000:
            return
        if self._risk_lockout_until_bar_idx > self._current_bar_idx:
            return
        previous = self._risk_lockout_until_bar_idx
        self._risk_lockout_until_bar_idx = -10_000
        self._record_event(
            intent="btc_hybrid_loss_lockout_exit",
            rationale="loss lockout window expired",
            outcome=Outcome.NOTED,
            source="bar_progress",
            bar_idx=self._current_bar_idx,
            previous_until_bar_idx=previous,
        )

    def _refresh_grid_snapshot(self, mid: float) -> None:
        previous_orders = {
            (float(order.price), self._coerce_side(order.side)): order
            for order in self.grid_state.active_orders
        }
        self.grid_state.levels = self._grid_target_levels(mid)
        orders = self.manage_grid(mid, self.grid_state)
        for order in orders:
            level = float(order.price)
            side = self._coerce_side(order.side)
            previous = previous_orders.get((level, side))
            active = level in self._grid_levels
            order.is_active = active
            if active:
                order.order_id = self._grid_levels.get(level)
                order.status_hint = self._grid_level_status.get(level, previous.status_hint if previous else "OPEN")
            elif previous is not None:
                order.order_id = previous.order_id
                order.status_hint = previous.status_hint
        self.grid_state.active_orders = orders

    def _resolve_mode(self, bar: dict[str, Any]) -> HybridMode:
        bar = self._bar_with_fallbacks(bar)
        self._refresh_tape_quality(bar)
        candidate = self._classify_mode(bar, profile=self.profile)
        adx = self._as_float(bar.get("adx_14"), 0.0)
        h = self.profile.mode_hysteresis_adx
        if self._market_quality_blocked:
            grid_below_floor = (
                candidate == HybridMode.GRID
                and self._execution_quality < self.profile.execution_quality_floor
            )
            directional_below_floor = (
                candidate == HybridMode.DIRECTIONAL
                and self._directional_quality < self.profile.directional_quality_floor
            )
            if grid_below_floor or directional_below_floor:
                return HybridMode.FLAT
        if self._mode == HybridMode.GRID:
            if adx >= self.profile.adx_trending_threshold + h:
                return HybridMode.DIRECTIONAL
            if adx <= self.profile.adx_ranging_threshold + h:
                return HybridMode.GRID
            return HybridMode.FLAT
        if self._mode == HybridMode.DIRECTIONAL:
            if adx <= self.profile.adx_ranging_threshold - h:
                return HybridMode.GRID
            if adx >= self.profile.adx_trending_threshold - h:
                return HybridMode.DIRECTIONAL
            return HybridMode.FLAT
        if candidate == HybridMode.GRID and adx <= self.profile.adx_ranging_threshold:
            return HybridMode.GRID
        if candidate == HybridMode.DIRECTIONAL and adx >= self.profile.adx_trending_threshold:
            return HybridMode.DIRECTIONAL
        return HybridMode.FLAT

    @staticmethod
    def _grid_side_for_signal(signal: Signal) -> Side:
        raw_side = signal.meta.get("grid_side")
        if isinstance(raw_side, str):
            try:
                return Side(raw_side.upper())
            except ValueError:
                pass
        mid = float(signal.meta.get("grid_mid", signal.price))
        return Side.BUY if signal.price < mid else Side.SELL

    def _sync_active_grid_order_snapshot(
        self,
        *,
        price: float,
        side: Side,
        is_active: bool,
        order_id: str | None = None,
        status_hint: str | None = None,
    ) -> None:
        """Mirror the grid arm / release state into ``grid_state``."""
        for order in self.grid_state.active_orders:
            if abs(float(order.price) - price) <= 1e-9 and self._coerce_side(order.side) == side:
                order.is_active = is_active
                if order_id is not None:
                    order.order_id = order_id
                if status_hint is not None:
                    order.status_hint = status_hint
                break

    def _remember_grid_level(
        self,
        *,
        price: float,
        side: Side,
        order_id: str,
        status_hint: str,
        record_event: bool,
        rationale: str,
        armed_bar_idx: int | None = None,
    ) -> bool:
        """Store a grid arm in the live tracking maps."""
        is_new = price not in self._grid_levels
        if is_new:
            self._grid_side_counts[side] += 1
        self._grid_levels[price] = order_id
        self._grid_level_side[price] = side
        self._grid_level_status[price] = status_hint
        if armed_bar_idx is not None:
            self._grid_level_armed_idx[price] = armed_bar_idx
        self._sync_active_grid_order_snapshot(
            price=price,
            side=side,
            is_active=True,
            order_id=order_id,
            status_hint=status_hint,
        )
        if record_event and is_new:
            self._record_event(
                intent="btc_hybrid_grid_arm",
                rationale=rationale,
                outcome=Outcome.EXECUTED,
                price=price,
                side=side.value,
                order_id=order_id,
            )
        return is_new

    def init_grid(self, price_high: float, price_low: float, *, bar: dict[str, Any] | None = None) -> None:
        """Seed the compatibility grid snapshot used by live supervisors."""
        self._grid_bounds_high = 0.0
        self._grid_bounds_low = 0.0
        self._grid_anchor_mid = 0.0
        self._current_bar_idx = 0
        self._loss_streak = 0
        self._throttle_mult = 1.0
        self._volatility_throttle_mult = 1.0
        self._last_directional_bar_idx = -10_000
        self._risk_lockout_until_bar_idx = -10_000
        self._set_launch_geometry_from_bar(bar)
        self.grid_state.levels = []
        self.grid_state.active_orders = []
        self.grid_state.filled_buys = 0
        self.grid_state.filled_sells = 0
        self._grid_levels.clear()
        self._grid_level_side.clear()
        self._grid_level_status.clear()
        self._grid_level_armed_idx.clear()
        self._grid_side_counts[Side.BUY] = 0
        self._grid_side_counts[Side.SELL] = 0
        if price_high <= 0.0 or price_low <= 0.0 or price_high <= price_low:
            return
        self._grid_bounds_high = price_high
        self._grid_bounds_low = price_low
        mid = (price_high + price_low) / 2.0
        self._grid_anchor_mid = mid
        self._refresh_grid_snapshot(mid)
        self.grid_state.filled_buys = 0
        self.grid_state.filled_sells = 0

    @property
    def armed_grid_count(self) -> int:
        """Return the number of currently armed grid levels."""
        return len(self._grid_levels)

    def manage_grid(
        self,
        current_price: float,
        grid_state: GridState | None = None,
    ) -> list[GridOrder]:
        """Build the compatibility grid order snapshot for a price."""
        state = grid_state or self.grid_state
        orders: list[GridOrder] = []
        if current_price <= 0.0:
            return orders
        temporal_mult = self._last_temporal_size_mult
        for idx, lvl in enumerate(state.levels):
            side = Side.BUY if lvl < current_price else Side.SELL
            size = (
                self.state.equity
                * self._active_grid_inventory_cap_pct
                / self.profile.grid_levels
            )
            qty = size / lvl if lvl > 0.0 else 0.0
            qty *= self._quality_signal_mult(self._execution_quality)
            qty *= max(0.85, min(1.15, 0.85 + self._last_order_book_quality * 0.015))
            qty *= self._inventory_side_mult(side)
            qty *= temporal_mult
            orders.append(
                GridOrder(
                    level_idx=idx,
                    price=lvl,
                    side=side.value,
                    size=qty,
                ),
            )
        return orders

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

    def _release_grid_level(
        self,
        *,
        price: float,
        side: Side | None = None,
        order_id: str | None = None,
        source: str,
    ) -> bool:
        """Release one armed grid level from a sweep or venue fill.

        ``order_id`` wins when present because it is the most precise
        reconciliation key. If it is unavailable we fall back to the
        price that the venue reported.
        """
        armed_price: float | None = None
        armed_order_id: str | None = None
        armed_side: Side | None = None

        if order_id:
            for candidate_price, candidate_order_id in self._grid_levels.items():
                if candidate_order_id == order_id:
                    armed_price = candidate_price
                    armed_order_id = candidate_order_id
                    armed_side = self._grid_level_side.get(candidate_price)
                    break

        if armed_price is None:
            armed_price = price
            armed_order_id = self._grid_levels.get(price)
            armed_side = self._grid_level_side.get(price)

        if armed_price is None or armed_side is None:
            return False
        if side is not None and armed_side != side:
            return False
        if order_id is None and armed_price != price:
            return False

        self._grid_levels.pop(armed_price, None)
        self._grid_level_side.pop(armed_price, None)
        self._grid_level_status.pop(armed_price, None)
        self._grid_level_armed_idx.pop(armed_price, None)
        if self._grid_side_counts[armed_side] > 0:
            self._grid_side_counts[armed_side] -= 1
        status_hint = "FILLED" if source in {"bar_sweep", "venue_fill"} else "EXPIRED"
        self._sync_active_grid_order_snapshot(
            price=armed_price,
            side=armed_side,
            is_active=False,
            status_hint=status_hint,
        )
        if armed_side == Side.BUY:
            self.grid_state.filled_buys += 1
        else:
            self.grid_state.filled_sells += 1
        self._record_event(
            intent="btc_hybrid_grid_fill",
            rationale=f"{armed_side.value} level released via {source}",
            outcome=Outcome.EXECUTED,
            price=armed_price,
            fill_price=price,
            side=armed_side.value,
            order_id=armed_order_id or "",
            source=source,
        )
        return True

    # ------------------------------------------------------------------
    # Regime -> mode switcher
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_mode(
        bar: dict[str, Any],
        *,
        profile: BtcHybridProfile | None = None,
    ) -> HybridMode:
        """Decide GRID vs DIRECTIONAL based on ADX + BB squeeze."""
        p = profile or BtcHybridProfile()
        adx: float = bar.get("adx_14", 0.0)
        if adx >= p.adx_trending_threshold:
            return HybridMode.DIRECTIONAL
        if adx <= p.adx_ranging_threshold:
            return HybridMode.GRID
        # Transition band: stay flat. This lets the bot hand off cleanly
        # between modes without double-booking risk across both legs.
        return HybridMode.FLAT

    @staticmethod
    def _infer_regime(
        bar: dict[str, Any],
        *,
        profile: BtcHybridProfile | None = None,
    ) -> RegimeType:
        p = profile or BtcHybridProfile()
        adx: float = bar.get("adx_14", 18.0)
        if adx >= p.adx_trending_threshold:
            return RegimeType.TRENDING
        if adx >= p.adx_ranging_threshold:
            return RegimeType.TRANSITION
        return RegimeType.RANGING

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        # Ask Jarvis for STRATEGY_DEPLOY permission BEFORE arming the
        # grid so a kill/stand-aside context denies the strategy before
        # a single order is placed.
        allowed, _cap, code = self._ask_jarvis(
            ActionType.STRATEGY_DEPLOY,
            rationale="arming L2 BTC hybrid bot",
            mode="hybrid",
            overnight_explicit=True,
        )
        if not allowed:
            logger.warning(
                "BTC-Hybrid refused to start: %s", code,
            )
            self._record_event(
                intent="btc_hybrid_start_blocked",
                rationale=f"jarvis refused STRATEGY_DEPLOY: {code}",
                outcome=Outcome.BLOCKED,
            )
            self.state.is_paused = True
            return
        logger.info(
            "BTC-Hybrid starting | symbol=%s cap=$%.0f router=%s profile=%s",
            self._venue_symbol, self.config.starting_capital_usd,
            "yes" if self._router is not None else "paper-sim",
            {
                "adx_range": self.profile.adx_ranging_threshold,
                "adx_trend": self.profile.adx_trending_threshold,
                "grid_levels": self.profile.grid_levels,
                "grid_spacing_pct": self.profile.grid_spacing_pct,
                "grid_cap_pct": self.profile.grid_inventory_cap_pct,
                "dir_floor": self.profile.dir_min_confluence,
            },
        )
        self._record_event(
            intent="btc_hybrid_start",
            rationale="jarvis approved STRATEGY_DEPLOY",
            outcome=Outcome.EXECUTED,
            symbol=self._venue_symbol,
            router="yes" if self._router is not None else "paper-sim",
            profile=asdict(self.profile),
        )

    async def stop(self) -> None:
        logger.info(
            "BTC-Hybrid stopping | equity=$%.2f mode=%s",
            self.state.equity, self._mode.value,
        )
        self._grid_levels.clear()
        self._grid_level_side.clear()
        self._grid_level_status.clear()
        self._grid_level_armed_idx.clear()
        self._grid_side_counts[Side.BUY] = 0
        self._grid_side_counts[Side.SELL] = 0
        self._recent_bars.clear()
        self._grid_anchor_mid = 0.0
        self._active_grid_spacing_pct = self.profile.grid_spacing_pct
        self._active_grid_inventory_cap_pct = self.profile.grid_inventory_cap_pct
        self._current_bar_idx = 0
        self._loss_streak = 0
        self._throttle_mult = 1.0
        self._volatility_throttle_mult = 1.0
        self._last_directional_bar_idx = -10_000
        self._risk_lockout_until_bar_idx = -10_000
        self._last_order_book_venue = ""
        self._last_order_book_depth = 0
        self._last_order_book_age_ms = 0.0
        self._last_order_book_depth_score = 5.0
        self._last_order_book_freshness_score = 5.0
        self._last_order_book_quality = 5.0
        self._last_order_book_quality_bucket = "Q4_6"
        self._last_session_timeframe_key = "UNKNOWN::UNKNOWN"
        self._mode = HybridMode.FLAT
        self._record_event(
            intent="btc_hybrid_stop",
            rationale="bot stopped and grid cleared",
            outcome=Outcome.NOTED,
            equity=self.state.equity,
        )

    # ------------------------------------------------------------------
    # Bar-level switcher: sets mode, dispatches to the active leg
    # ------------------------------------------------------------------

    async def on_bar(self, bar: dict[str, Any]) -> None:
        if not self.check_risk():
            return
        bar_idx = self._bar_index_for(bar)
        self._append_recent_bar(bar)
        bar = self._bar_with_fallbacks(bar)
        self._refresh_tape_quality(bar)
        self._refresh_runtime_throttle(bar)
        self._refresh_risk_lockout()
        new_mode = self._resolve_mode(bar)
        if new_mode != self._mode:
            logger.info(
                "BTC-Hybrid mode %s -> %s (adx=%.1f)",
                self._mode.value, new_mode.value,
                bar.get("adx_14", 0.0),
            )
            self._record_event(
                intent="btc_hybrid_mode_change",
                rationale=f"{self._mode.value} -> {new_mode.value}",
                outcome=Outcome.NOTED,
                adx=float(bar.get("adx_14", 0.0)),
                bar_idx=bar_idx,
            )
            self._mode = new_mode
        if self._mode == HybridMode.GRID:
            await self._tick_grid(bar)
        elif self._mode == HybridMode.DIRECTIONAL:
            await self._tick_directional(bar)
        # FLAT: intentional no-op; transition band is a REST state.

    # ------------------------------------------------------------------
    # GRID leg
    # ------------------------------------------------------------------

    async def _tick_grid(self, bar: dict[str, Any]) -> None:
        """Place / maintain the grid around the range midpoint.
        """
        bar = self._bar_with_fallbacks(bar)
        mid = (bar.get("bb_upper", 0.0) + bar.get("bb_lower", 0.0)) / 2
        if mid <= 0.0:
            return
        if self._grid_anchor_mid <= 0.0:
            self._grid_anchor_mid = mid
        self._reconcile_grid_fills(bar)
        self._expire_stale_grid_levels(current_mid=mid)
        self._refresh_grid_snapshot(self._grid_anchor_mid)
        if self._risk_lockout_active():
            return
        if self._market_quality_blocked or self._execution_quality < self.profile.execution_quality_floor:
            self._record_event(
                intent="btc_hybrid_grid_quality_block",
                rationale=self._quality_block_reason(),
                outcome=Outcome.BLOCKED,
                mid=mid,
                market_quality=self._market_quality,
                execution_quality=self._execution_quality,
            )
            return
        target_levels = list(self.grid_state.levels)
        # Only place levels we haven't already armed.
        missing = [p for p in target_levels if p not in self._grid_levels]
        if not missing:
            return
        # Submit the missing levels as a batch of GRID_ADD signals. Each
        # one still flows through JARVIS -- the grid is technically
        # "risk-adding" every time a new price level is armed.
        for px in missing:
            side = Side.BUY if px < mid else Side.SELL
            if self._grid_side_counts[side] >= (self.profile.grid_levels // 2):
                self._record_event(
                    intent="btc_hybrid_grid_cap_block",
                    rationale=f"{side.value} side already at cap",
                    outcome=Outcome.BLOCKED,
                    price=px,
                    mid=mid,
                    side=side.value,
                )
                continue
            sig = Signal(
                type=SignalType.GRID_ADD, symbol=self.config.symbol,
                price=px,
                confidence=min(
                    10.0,
                    5.5 + (self._execution_quality - self.profile.execution_quality_floor) * 3.0,
                ),
                meta={
                    "grid_mid": mid,
                    "mode": "grid",
                    "grid_side": side.value,
                    "market_quality": self._market_quality,
                    "execution_quality": self._execution_quality,
                    "quality_mult": self._quality_signal_mult(self._execution_quality),
                    "temporal_size_mult": self._last_temporal_size_mult,
                },
            )
            await self.on_signal(sig)

    def _reconcile_grid_fills(self, bar: dict[str, Any]) -> int:
        """Release any armed grid levels that the current bar has swept.

        The bot does not get a live fill stream here, so we treat a sweep /
        reclaim through the level as the fill signal and remove the arm.
        The next grid tick can then re-arm the now-missing level on the
        current side of the tape.
        """
        if not self._grid_levels:
            return 0
        released = 0
        high = float(bar.get("high", bar.get("close", 0.0)))
        low = float(bar.get("low", bar.get("close", 0.0)))
        close = float(bar.get("close", 0.0))
        for price, order_id in list(self._grid_levels.items()):
            side = self._grid_level_side.get(price)
            if side is None:
                continue
            touched = False
            if side == Side.BUY and low <= price <= close or side == Side.SELL and close <= price <= high:
                touched = True
            if not touched:
                continue
            if self._release_grid_level(
                price=price,
                side=side,
                order_id=order_id or None,
                source="bar_sweep",
            ):
                released += 1
        return released

    def _rehydrate_active_grid_orders(self) -> None:
        """Reapply stored order ids to the compatibility grid snapshot."""
        for order in self.grid_state.active_orders:
            if not order.is_active:
                continue
            order_id = self._grid_levels.get(float(order.price))
            if order_id:
                order.order_id = order_id
                order.status_hint = self._grid_level_status.get(float(order.price), order.status_hint)

    def _grid_target_levels(self, mid: float) -> list[float]:
        """Build the target price grid symmetric around ``mid``."""
        levels: list[float] = []
        half = self.profile.grid_levels // 2
        spacing = self._active_grid_spacing_pct
        for i in range(1, half + 1):
            levels.append(mid * (1 - spacing * i))
            levels.append(mid * (1 + spacing * i))
        return levels

    def _expire_stale_grid_levels(self, *, current_mid: float) -> int:
        if not self._grid_levels:
            return 0
        released = 0
        current_idx = self._current_bar_idx
        for price, order_id in list(self._grid_levels.items()):
            armed_idx = self._grid_level_armed_idx.get(price, current_idx)
            age = current_idx - armed_idx
            drift_pct = abs(current_mid - price) / price if price > 0.0 else 0.0
            if age < self.profile.grid_stale_bars and drift_pct < self.profile.grid_reanchor_drift_pct:
                continue
            side = self._grid_level_side.get(price)
            if self._release_grid_level(
                price=price,
                side=side,
                order_id=order_id or None,
                source="stale_cleanup",
            ):
                released += 1
        if (
            released > 0
            and not self._grid_levels
            and self._grid_anchor_mid > 0.0
            and abs(current_mid - self._grid_anchor_mid) / self._grid_anchor_mid
                >= self.profile.grid_reanchor_drift_pct
        ):
                self._grid_anchor_mid = current_mid
                self._record_event(
                    intent="btc_hybrid_grid_reanchor",
                    rationale="grid book cleared after drift; anchor moved to current mid",
                    outcome=Outcome.NOTED,
                    price=current_mid,
                    bar_idx=current_idx,
                )
        return released

    # ------------------------------------------------------------------
    # DIRECTIONAL leg
    # ------------------------------------------------------------------

    async def _tick_directional(self, bar: dict[str, Any]) -> None:
        """Emit a directional signal if confluence clears the floor."""
        bar = self._bar_with_fallbacks(bar)
        if self._risk_lockout_active():
            return
        if self._market_quality_blocked or self._directional_quality < self.profile.directional_quality_floor:
            self._record_event(
                intent="btc_hybrid_directional_quality_block",
                rationale=self._quality_block_reason(),
                outcome=Outcome.BLOCKED,
                bar_idx=self._current_bar_idx,
            )
            return
        if self.profile.directional_cooldown_bars > 0:
            bars_since_dir = self._current_bar_idx - self._last_directional_bar_idx
            if 0 <= bars_since_dir <= self.profile.directional_cooldown_bars:
                self._record_event(
                    intent="btc_hybrid_directional_cooldown",
                    rationale="cooldown window active",
                    outcome=Outcome.NOTED,
                    bar_idx=self._current_bar_idx,
                    bars_since_directional=bars_since_dir,
                )
                return
        breaker = self._breaker_retest_signal(bar)
        if breaker is not None:
            sig = Signal(
                type=SignalType.LONG if breaker.side is StrategySide.LONG else SignalType.SHORT,
                symbol=self.config.symbol,
                price=float(breaker.entry or bar.get("close", 0.0)),
                confidence=min(
                    10.0,
                    max(float(breaker.confidence), self.profile.dir_min_confluence)
                    + max(0.0, (self._directional_quality - self.profile.directional_quality_floor) * 4.0),
                ),
                meta={
                    "mode": "directional",
                    "strategy": breaker.strategy.value,
                    "strategy_tags": ",".join(breaker.rationale_tags),
                    "stop_distance": abs(float(breaker.entry) - float(breaker.stop)),
                    "size_mult": max(self.profile.quality_size_floor, min(1.5, float(breaker.risk_mult or 1.0))),
                    "temporal_size_mult": self._last_temporal_size_mult,
                    "entry_price": float(breaker.entry),
                    "stop_price": float(breaker.stop),
                    "target_price": float(breaker.target),
                    "market_quality": self._market_quality,
                    "directional_quality": self._directional_quality,
                    "order_book_age_ms": self._last_order_book_age_ms,
                    "order_book_depth_score": self._last_order_book_depth_score,
                    "order_book_freshness_score": self._last_order_book_freshness_score,
                    "order_book_quality": self._last_order_book_quality,
                },
            )
            self._last_directional_bar_idx = self._current_bar_idx
            await self.on_signal(sig)
            return
        ema_9 = bar.get("ema_9", 0.0)
        ema_21 = bar.get("ema_21", 0.0)
        if ema_9 == 0.0 or ema_21 == 0.0:
            return
        direction = SignalType.LONG if ema_9 > ema_21 else SignalType.SHORT
        # Simple confluence: ADX + EMA separation + volume spike
        vol_ratio = bar.get("volume", 0) / max(bar.get("avg_volume", 1), 1)
        ema_spread = abs(ema_9 - ema_21) / max(ema_21, 1)
        confluence = min(
            6.0
            + (bar.get("adx_14", 0) - self.profile.adx_trending_threshold) / 10
            + ema_spread * 100 + max(0.0, vol_ratio - 1.0),
            10.0,
        )
        if self._directional_quality < self.profile.directional_quality_floor:
            return
        if confluence < self.profile.dir_min_confluence:
            return
        sig = Signal(
            type=direction, symbol=self.config.symbol,
            price=bar["close"], confidence=confluence,
            meta={
                "mode": "directional",
                "strategy": "ema_adx_volume",
                "stop_distance": bar["close"] * 0.01,
                "size_mult": max(
                    self.profile.quality_size_floor,
                    min(1.5, 1.0 + max(0.0, confluence - self.profile.dir_min_confluence) * 0.05),
                ),
                "temporal_size_mult": self._last_temporal_size_mult,
                "market_quality": self._market_quality,
                "directional_quality": self._directional_quality,
                "order_book_age_ms": self._last_order_book_age_ms,
                "order_book_depth_score": self._last_order_book_depth_score,
                "order_book_freshness_score": self._last_order_book_freshness_score,
                "order_book_quality": self._last_order_book_quality,
            },
        )
        self._last_directional_bar_idx = self._current_bar_idx
        await self.on_signal(sig)

    # ------------------------------------------------------------------
    # Shared on_signal -- JARVIS gate is here
    # ------------------------------------------------------------------

    async def on_signal(self, signal: Signal) -> OrderResult | None:
        """Route a signal through JARVIS and, if allowed, to the venue."""
        if signal.type in {SignalType.LONG, SignalType.SHORT, SignalType.GRID_ADD} and self._risk_lockout_active():
            self._record_event(
                intent="btc_hybrid_loss_lockout_block",
                rationale="loss lockout blocked new risk",
                outcome=Outcome.BLOCKED,
                signal=signal.type.value,
                bar_idx=self._current_bar_idx,
            )
            return None
        allowed, cap, code = self._ask_jarvis(
            ActionType.ORDER_PLACE,
            rationale=f"{signal.type.value} {signal.meta.get('mode', '?')}",
            side=signal.type.value,
            symbol=signal.symbol,
            price=signal.price,
            confidence=signal.confidence,
            overnight_explicit=True,
        )
        if not allowed:
            return None
        qty = self._size_from_signal(signal, size_cap_mult=cap)
        if qty <= 0.0:
            logger.debug(
                "BTC-Hybrid size<=0 -- skipping %s @ %.2f",
                signal.type.value, signal.price,
            )
            return None
        if self._router is None:
            logger.info(
                "BTC-Hybrid paper-sim %s %s qty=%.6f @ %.2f (cap=%s)",
                signal.type.value, signal.symbol, qty, signal.price,
                f"{cap:.2f}" if cap is not None else "none",
            )
            # In paper-sim we still record the arm so the grid tick
            # doesn't keep re-submitting the same price level on each
            # bar. The empty string stands in for a venue order id.
            if signal.type == SignalType.GRID_ADD:
                side = self._grid_side_for_signal(signal)
                self._remember_grid_level(
                    price=signal.price,
                    side=side,
                    order_id="",
                    status_hint="OPEN",
                    record_event=True,
                    rationale="paper-sim grid level armed",
                    armed_bar_idx=self._current_bar_idx,
                )
            else:
                self._last_directional_bar_idx = self._current_bar_idx
            return None
        if signal.type == SignalType.GRID_ADD:
            side = self._grid_side_for_signal(signal)
            reduce_only = False
            order_type = OrderType.LIMIT
        else:
            side, reduce_only = _signal_to_side(signal.type)
            order_type = OrderType.MARKET
        req = OrderRequest(
            symbol=self._venue_symbol, side=side, qty=qty,
            order_type=order_type,
            reduce_only=reduce_only,
        )
        try:
            result = await self._router.place_with_failover(req)
        except Exception as e:  # noqa: BLE001 - router alerts internally
            logger.error("BTC-Hybrid route failed: %s", e)
            return None
        if result.status is OrderStatus.REJECTED:
            logger.warning(
                "BTC-Hybrid order rejected: id=%s reason_code=%s",
                result.order_id, code,
            )
        elif signal.type == SignalType.GRID_ADD:
            grid_side = self._grid_side_for_signal(signal)
            self._remember_grid_level(
                price=signal.price,
                side=grid_side,
                order_id=result.order_id or "",
                status_hint=result.status.value,
                record_event=result.status is not OrderStatus.FILLED,
                rationale="grid level armed",
                armed_bar_idx=self._current_bar_idx,
            )
            if result.status is OrderStatus.FILLED:
                fill = Fill(
                    symbol=self.config.symbol,
                    side=grid_side.value,
                    price=float(result.avg_price or signal.price),
                    size=float(result.filled_qty or qty),
                    fee=float(result.fees or 0.0),
                    realized_pnl=0.0,
                )
                self.record_fill(
                    fill,
                    order_id=result.order_id or None,
                    side=grid_side,
                )
        else:
            self._last_directional_bar_idx = self._current_bar_idx
        return result

    def record_fill(
        self,
        fill: Fill,
        *,
        order_id: str | None = None,
        side: Side | str | None = None,
    ) -> bool:
        """Process a venue fill and reconcile any armed grid level.

        This gives the live pipeline a direct callback once a broker or
        simulator reports a fill. The current candle-based loop keeps its
        sweep reconciliation, but venue-side fills now have a first-class
        path too.

        Returns True when an armed grid level was released.
        """
        if fill.symbol not in (self.config.symbol, self._venue_symbol):
            return False
        self.update_state(fill)
        delta = float(fill.realized_pnl) - float(fill.fee)
        if abs(float(fill.realized_pnl)) > 1e-12 or float(fill.risk_at_entry) > 0.0:
            if delta < 0.0:
                self._loss_streak += 1
            elif delta > 0.0:
                self._loss_streak = 0
            self._refresh_runtime_throttle({})
            self._enter_loss_lockout(source="venue_fill")
        fill_side = self._coerce_side(side) or self._coerce_side(fill.side)
        return self._release_grid_level(
            price=float(fill.price),
            side=fill_side,
            order_id=order_id or None,
            source="venue_fill",
        )

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------

    def _size_from_signal(
        self, signal: Signal, *, size_cap_mult: float | None,
    ) -> float:
        """Derive coin qty from risk-per-trade, with optional Jarvis cap."""
        if signal.type == SignalType.GRID_ADD:
            # Grid orders use an inventory-cap allocation: equity * cap
            # / grid_levels. Each level is a small nibble; collectively
            # they respect _GRID_INVENTORY_CAP_PCT per side.
            per_level = (
                self.state.equity
                * self._active_grid_inventory_cap_pct
                / self.profile.grid_levels
            )
            qty = per_level / max(signal.price, 1.0)
            qty *= self._quality_signal_mult(self._execution_quality)
        else:
            risk_usd = (
                self.state.equity * (self.config.risk_per_trade_pct / 100.0)
            )
            stop_distance = signal.meta.get(
                "stop_distance", signal.price * 0.01,
            )
            qty = (
                risk_usd / stop_distance if stop_distance > 0 else 0.0
            )
        size_mult = float(signal.meta.get("size_mult", 1.0) or 1.0)
        temporal_mult = float(signal.meta.get("temporal_size_mult", self._last_temporal_size_mult) or 1.0)
        book_mult = max(0.85, min(1.15, 0.85 + self._last_order_book_quality * 0.015))
        freshness_mult = max(0.92, min(1.08, 0.92 + self._last_order_book_freshness_score * 0.012))
        if size_mult > 0.0:
            qty *= size_mult
        if temporal_mult > 0.0:
            qty *= temporal_mult
        qty *= book_mult
        qty *= freshness_mult
        if signal.type in {SignalType.LONG, SignalType.SHORT}:
            qty *= self._quality_signal_mult(self._directional_quality)
        qty *= self._inventory_signal_mult(signal)
        qty *= self._throttle_mult
        if size_cap_mult is not None:
            qty *= size_cap_mult
        return round(max(qty, 0.0), 6)

    # ------------------------------------------------------------------
    # Entry / exit decision hooks (required by BaseBot)
    # ------------------------------------------------------------------

    def evaluate_entry(
        self, bar: dict[str, Any], confluence_score: float,
    ) -> bool:
        """Entry permitted if confluence >= floor and risk checks pass.

        The mode dispatch (GRID vs DIRECTIONAL) happens inside
        ``on_bar``; this hook only answers "is the top-level bot alive"
        which is effectively ``check_risk()``.
        """
        return (
            confluence_score >= self.profile.dir_min_confluence
            and self.check_risk()
            and not self._risk_lockout_active()
        )

    def evaluate_exit(self, position: Position) -> bool:
        """Exit on 1R loss or 2R gain (BTC hybrid uses a lower R-multiple
        target than the L3 perps because the grid side generates more
        frequent small wins)."""
        risk_usd = (
            self.config.risk_per_trade_pct / 100 * self.state.equity
        )
        if position.unrealized_pnl <= -risk_usd:
            return True
        return position.unrealized_pnl >= 2.0 * risk_usd


def _signal_to_side(sig_type: SignalType) -> tuple[Side, bool]:
    if sig_type in (SignalType.LONG, SignalType.GRID_ADD):
        return Side.BUY, False
    if sig_type in (SignalType.SHORT, SignalType.GRID_REMOVE):
        return Side.SELL, False
    if sig_type == SignalType.CLOSE_LONG:
        return Side.SELL, True
    if sig_type == SignalType.CLOSE_SHORT:
        return Side.BUY, True
    return Side.BUY, False
