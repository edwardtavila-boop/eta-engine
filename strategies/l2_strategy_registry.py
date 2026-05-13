"""
EVOLUTIONARY TRADING ALGO  //  strategies.l2_strategy_registry
==============================================================
Curated registry of L2-aware (depth-consuming) strategies, kept
SEPARATE from ``per_bot_registry`` which is bar-shaped.

Why a separate registry
-----------------------
The strategies in this file consume *depth snapshots* + *ticks*,
not OHLCV bars.  Their data shape is different from the legacy
fleet's StrategyAssignment which assumes bar-based replay.  Forcing
the L2 strategies into per_bot_registry would bloat that file with
adapter shims that hide more bugs than they prevent.

Each entry here is what an operator needs to wire one of the L2
strategies into the live fleet:

  - factory: a callable that builds the strategy instance
  - symbol: the symbol the strategy is calibrated for
  - capture_required: True when the strategy WILL NOT produce
    signals unless Phase 1 capture is running
  - promotion_status: "shadow" | "paper" | "live"
  - sizing_policy: hard-capped contract count + R-loss limit
  - falsification: pre-committed criteria for when to retire

Integration with the live fleet
-------------------------------
At session start, the order router calls
``l2_strategy_registry.iter_active_l2_strategies()`` and for each
entry:
  1. Constructs the strategy via the factory
  2. Calls ``mark_captures_expected(symbol)`` if capture_required
     (so the overlay fails CLOSED instead of OPEN on missing data)
  3. Subscribes to the depth snapshot stream for the symbol
  4. Routes signals through ``trading_gate.check_pre_trade_gate``
     before placing broker orders

Promotion gate
--------------
Promotion from shadow → paper requires:
  - 14 days of clean capture (no MISSING/STALE alerts)
  - n_trades >= 30 across the period
  - bootstrap-CI win_rate lower bound > 0.50

Promotion from paper → live requires additionally:
  - walk_forward.test.sharpe_proxy >= 0.5
  - walk_forward.promotion_gate.passes == True
  - No risk-execution issues in the paper-soak alerts log

These criteria are checked by the supercharge orchestrator and
materialized as per-bot verdicts.
"""

from __future__ import annotations

# ruff: noqa: ANN401
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class L2StrategyEntry:
    """One L2-aware strategy registration."""

    bot_id: str
    strategy_id: str
    symbol: str
    factory: Callable[[], Any]
    capture_required: bool  # True when no depth = no signals
    promotion_status: str  # "shadow" | "paper" | "live" | "deactivated"
    # Sizing policy — hard-capped, NEVER derived from equity
    max_qty_contracts: int = 1
    max_daily_loss_dollars: float = 200.0  # circuit breaker
    # Falsification criteria — pre-committed retirement triggers
    falsification: dict[str, Any] = field(default_factory=dict)
    # Free-form notes for the operator
    rationale: str = ""


# ── Strategy factories ────────────────────────────────────────────


def _factory_book_imbalance() -> Any:
    from eta_engine.strategies.book_imbalance_strategy import (
        BookImbalanceConfig,
        make_book_imbalance_strategy,
    )

    cfg = BookImbalanceConfig(
        n_levels=3,
        entry_threshold=1.75,
        consecutive_snaps=3,
        atr_stop_mult=1.0,
        rr_target=2.0,
        cooldown_bars=3,
        snapshot_interval_seconds=5.0,
        max_trades_per_day=6,
        max_qty_contracts=1,
    )
    return make_book_imbalance_strategy(cfg, symbol="MNQ")


def _factory_spread_regime_filter() -> Any:
    from eta_engine.strategies.spread_regime_filter import (
        SpreadRegimeConfig,
        make_spread_regime_filter,
    )

    return make_spread_regime_filter(SpreadRegimeConfig())


def _factory_footprint_absorption() -> Any:
    from eta_engine.strategies.footprint_absorption_strategy import (
        FootprintAbsorptionConfig,
        make_footprint_strategy,
    )

    return make_footprint_strategy(
        FootprintAbsorptionConfig(
            prints_size_z_min=1.5,
            absorption_ratio=0.5,
            absorb_price_band_ticks=2.0,
            cooldown_seconds=120.0,
        ),
        symbol="MNQ",
    )


def _factory_aggressor_flow() -> Any:
    from eta_engine.strategies.aggressor_flow_strategy import (
        AggressorFlowConfig,
        make_aggressor_flow_strategy,
    )

    return make_aggressor_flow_strategy(
        AggressorFlowConfig(
            window_bars=10,
            entry_threshold=0.35,
            consecutive_bars=2,
            cooldown_seconds=300.0,
        ),
        symbol="MNQ",
    )


def _factory_microprice_drift() -> Any:
    from eta_engine.strategies.microprice_drift_strategy import (
        MicropriceConfig,
        make_microprice_strategy,
    )

    return make_microprice_strategy(
        MicropriceConfig(
            drift_threshold_ticks=2.0,
            consecutive_snaps=3,
            cooldown_seconds=60.0,
            snapshot_interval_seconds=5.0,
        ),
        symbol="MNQ",
    )


# ── Registry ──────────────────────────────────────────────────────


L2_STRATEGIES: tuple[L2StrategyEntry, ...] = (
    L2StrategyEntry(
        bot_id="mnq_book_imbalance_shadow",
        strategy_id="book_imbalance_v1",
        symbol="MNQ",
        factory=_factory_book_imbalance,
        capture_required=True,
        promotion_status="shadow",
        max_qty_contracts=1,
        max_daily_loss_dollars=200.0,
        falsification={
            "retire_if_oos_sharpe_lt": 0.0,
            "retire_if_oos_n_trades_lt": 30,
            "retire_after_n_days_shadow_loss": 14,
            "time_horizon_days": 60,
        },
        rationale=(
            "Phase 4 primary L2 strategy.  Top-N bid/ask qty ratio "
            "with 3-snap hysteresis.  Promotion to paper requires "
            "walk-forward OOS sharpe >= 0.5 over 30+ trades."
        ),
    ),
    L2StrategyEntry(
        bot_id="mnq_spread_regime_filter",
        strategy_id="spread_regime_filter_v1",
        symbol="MNQ",
        factory=_factory_spread_regime_filter,
        capture_required=True,
        promotion_status="shadow",
        max_qty_contracts=0,  # filter, not entry strategy
        falsification={
            "retire_if_never_pauses_for_n_days": 30,
            "retire_if_pauses_more_than_pct_of_time": 0.40,
        },
        rationale=(
            "Phase 4 partner — global pause-trading-when-spread-blows-out "
            "filter.  Applied across all L2 strategies, not a standalone "
            "entry.  Promotion to paper means it's wired into the live "
            "order router as a gate."
        ),
    ),
    L2StrategyEntry(
        bot_id="mnq_footprint_absorption_shadow",
        strategy_id="footprint_absorption_v1",
        symbol="MNQ",
        factory=_factory_footprint_absorption,
        capture_required=True,
        promotion_status="shadow",
        max_qty_contracts=1,
        max_daily_loss_dollars=200.0,
        falsification={
            "retire_if_oos_sharpe_lt": 0.0,
            "retire_if_oos_n_trades_lt": 20,  # rarer signal
            "retire_after_n_days_shadow_loss": 21,
            "time_horizon_days": 90,
        },
        rationale=(
            "Phase 4 microstructure strategy.  Detects large aggressor "
            "prints absorbed by hidden liquidity.  Lower trade frequency "
            "than book_imbalance, so n_trades min is reduced to 20 and "
            "the retirement window is wider (21 days)."
        ),
    ),
    L2StrategyEntry(
        bot_id="mnq_aggressor_flow_shadow",
        strategy_id="aggressor_flow_v1",
        symbol="MNQ",
        factory=_factory_aggressor_flow,
        capture_required=False,  # consumes bars, not depth
        promotion_status="shadow",
        max_qty_contracts=1,
        max_daily_loss_dollars=200.0,
        falsification={
            "retire_if_oos_sharpe_lt": 0.0,
            "retire_if_oos_n_trades_lt": 30,
            "retire_after_n_days_shadow_loss": 14,
            "time_horizon_days": 60,
        },
        rationale=(
            "Phase 4 order-flow strategy.  Uses buy/sell-split volume "
            "from bar_builder_l1 to trade sustained aggressor pressure. "
            "Does NOT require depth captures — runs off L1 bars only."
        ),
    ),
    L2StrategyEntry(
        bot_id="mnq_microprice_drift_shadow",
        strategy_id="microprice_drift_v1",
        symbol="MNQ",
        factory=_factory_microprice_drift,
        capture_required=True,
        promotion_status="shadow",
        max_qty_contracts=1,
        max_daily_loss_dollars=200.0,
        falsification={
            "retire_if_oos_sharpe_lt": 0.0,
            "retire_if_oos_n_trades_lt": 30,
            "retire_after_n_days_shadow_loss": 14,
            "time_horizon_days": 60,
        },
        rationale=(
            "Phase 4 microprice strategy.  Trades dislocations between "
            "qty-weighted microprice and last trade print.  Tight "
            "scalping horizon — wide stops would defeat the edge."
        ),
    ),
)


def iter_active_l2_strategies(
    *,
    statuses: tuple[str, ...] = ("shadow", "paper", "live"),
) -> tuple[L2StrategyEntry, ...]:
    """Iterate L2 strategies currently in one of the given statuses.
    Default returns everything not 'deactivated'."""
    return tuple(s for s in L2_STRATEGIES if s.promotion_status in statuses)


def get_l2_strategy(bot_id: str) -> L2StrategyEntry | None:
    for s in L2_STRATEGIES:
        if s.bot_id == bot_id:
            return s
    return None


def required_capture_symbols(
    *,
    statuses: tuple[str, ...] = ("shadow", "paper", "live"),
) -> tuple[str, ...]:
    """Return the distinct symbols whose capture daemon MUST be alive
    for at least one active L2 strategy.  Caller passes these to
    ``mark_captures_expected()`` at session start."""
    symbols = set()
    for s in L2_STRATEGIES:
        if s.promotion_status not in statuses:
            continue
        if s.capture_required:
            symbols.add(s.symbol)
    return tuple(sorted(symbols))


def session_start_hook(
    *,
    when: datetime | None = None,
    statuses: tuple[str, ...] = ("shadow", "paper", "live"),
) -> dict:
    """Call this once at the start of each trading session.

    1. Marks captures_expected for every symbol that has at least
       one active L2 strategy with capture_required=True
    2. Returns a summary dict the order router can log

    After this call, the l2_overlay gates fail CLOSED on missing/
    stale depth data — protecting the live fleet from silent
    degradation when a capture daemon dies mid-session.
    """
    from eta_engine.strategies.l2_overlay import mark_captures_expected

    when = when or datetime.now(UTC)
    symbols = required_capture_symbols(statuses=statuses)
    for sym in symbols:
        mark_captures_expected(sym, when=when)
    active = iter_active_l2_strategies(statuses=statuses)
    summary = {
        "ts": when.isoformat(),
        "symbols_marked_expected": list(symbols),
        "n_active_strategies": len(active),
        "active_bot_ids": [s.bot_id for s in active],
    }
    return summary
