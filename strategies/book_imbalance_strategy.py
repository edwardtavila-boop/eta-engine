"""
EVOLUTIONARY TRADING ALGO  //  strategies.book_imbalance_strategy
=================================================================
Phase-4 of the IBKR Pro upgrade path: book-imbalance entry strategy.

Why this exists
---------------
Per docs/IBKR_PRO_DATA_INVENTORY.md Phase 4:
> book_imbalance_strategy.py — entry when top-3-level bid/ask
> imbalance > threshold for N consecutive ticks.  Backtest on the
> tick history accumulated in Phase 1.

Mechanic
--------
Read the current depth snapshot (top-N levels each side).  Compute:
    imbalance_ratio = sum(bid_qty[:N]) / sum(ask_qty[:N])

LONG signal: imbalance_ratio >= entry_threshold for k consecutive
             snapshots → price has 1.5-2x more bids than asks at
             top of book → anticipate uptick
SHORT signal: imbalance_ratio <= 1/entry_threshold for k consecutive
             snapshots → mirror

Stop: ATR-based or 1 tick beyond NBBO opposite side at signal time
Target: scaled by current spread (tighter spread → smaller target)

Phase-4 scaffolding policy
--------------------------
This module is the strategy LOGIC; it consumes depth snapshots via
the same loader pattern as l2_overlay.  It does NOT auto-promote
to live — operator must manually wire it into per_bot_registry
when paper-soak proves edge on real captured depth data.
"""
# ruff: noqa: ANN401
# typing.Any is correct for the strategy-factory return — different
# concrete classes have different evaluate() signatures.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class BookImbalanceConfig:
    """Tuning surface for book_imbalance entries."""
    n_levels: int = 3                  # depth-of-book rows per side
    entry_threshold: float = 1.75      # min bid/ask ratio for LONG (or inverse for SHORT)
    consecutive_snaps: int = 3         # snapshots required at threshold
    atr_stop_mult: float = 1.0         # stop = entry ± atr * mult
    rr_target: float = 2.0             # 2x stop distance to target
    cooldown_bars: int = 3             # don't re-enter within N bars after exit
    max_trades_per_day: int = 6
    spread_min_ticks: float = 1.0      # don't enter when spread is at min (illiquid)
    spread_max_ticks: float = 3.0      # don't enter when spread is wide (regime change)


@dataclass
class BookImbalanceState:
    """Per-bot state carried between bars."""
    consecutive_long_count: int = 0
    consecutive_short_count: int = 0
    last_signal_dt: datetime | None = None
    trades_today: int = 0
    today_str: str = ""
    last_snapshot: dict | None = None


@dataclass
class ImbalanceSignal:
    """What the strategy emits when an entry condition fires."""
    side: str            # "LONG" | "SHORT"
    entry_price: float
    stop: float
    target: float
    confidence: float    # 0.0 - 1.0 based on how far ratio exceeded threshold
    rationale: str       # one-line summary for the journal
    snapshot_ts: str     # ISO timestamp of the deciding snapshot


def compute_imbalance(snapshot: dict, n_levels: int) -> tuple[float, int, int]:
    """Return (ratio, bid_qty, ask_qty) from top-N levels of a snapshot.

    ratio = bid_qty / ask_qty.  Returns ratio=1.0 (neutral) when one
    side has zero qty — caller treats as "no signal" rather than
    division-by-zero infinity."""
    bids = snapshot.get("bids", [])[:n_levels]
    asks = snapshot.get("asks", [])[:n_levels]
    bid_qty = sum(int(lv.get("size", 0)) for lv in bids)
    ask_qty = sum(int(lv.get("size", 0)) for lv in asks)
    if bid_qty == 0 or ask_qty == 0:
        return 1.0, bid_qty, ask_qty
    return bid_qty / ask_qty, bid_qty, ask_qty


def evaluate_snapshot(snapshot: dict, config: BookImbalanceConfig,
                      state: BookImbalanceState, *,
                      atr: float = 1.0) -> ImbalanceSignal | None:
    """Process one depth snapshot.  Updates `state`.  Returns a
    signal when the consecutive-snapshot threshold is hit, else None.

    Pure function over (snapshot, config, state) — easy to unit-test
    without IBKR connectivity."""
    today = datetime.now(UTC).strftime("%Y%m%d")
    if state.today_str != today:
        state.today_str = today
        state.trades_today = 0

    if state.trades_today >= config.max_trades_per_day:
        return None

    # Spread filter
    spread = snapshot.get("spread", 0.0)
    if spread < config.spread_min_ticks * 0.25:  # 0.25 = MNQ tick size; generic floor
        return None
    if spread > config.spread_max_ticks * 0.25:
        return None

    ratio, bid_qty, ask_qty = compute_imbalance(snapshot, config.n_levels)
    state.last_snapshot = snapshot
    mid = float(snapshot.get("mid", 0.0))

    # LONG signal — bid side dominant for k snaps
    if ratio >= config.entry_threshold:
        state.consecutive_long_count += 1
        state.consecutive_short_count = 0
        if state.consecutive_long_count >= config.consecutive_snaps:
            entry = mid + spread / 2  # cross spread to take ask
            stop = entry - atr * config.atr_stop_mult
            target = entry + atr * config.atr_stop_mult * config.rr_target
            confidence = min(1.0, (ratio - config.entry_threshold) /
                              max(config.entry_threshold, 0.01))
            state.consecutive_long_count = 0
            state.last_signal_dt = datetime.now(UTC)
            state.trades_today += 1
            return ImbalanceSignal(
                side="LONG", entry_price=round(entry, 4),
                stop=round(stop, 4), target=round(target, 4),
                confidence=round(confidence, 2),
                rationale=f"book imbalance {ratio:.2f}x bid:ask "
                          f"({bid_qty}:{ask_qty}) for {config.consecutive_snaps} snaps",
                snapshot_ts=str(snapshot.get("ts", "")),
            )
    # SHORT signal — ask side dominant
    elif ratio <= 1.0 / config.entry_threshold:
        state.consecutive_short_count += 1
        state.consecutive_long_count = 0
        if state.consecutive_short_count >= config.consecutive_snaps:
            entry = mid - spread / 2  # cross spread to take bid
            stop = entry + atr * config.atr_stop_mult
            target = entry - atr * config.atr_stop_mult * config.rr_target
            inv_ratio = 1.0 / max(ratio, 0.01)
            confidence = min(1.0, (inv_ratio - config.entry_threshold) /
                              max(config.entry_threshold, 0.01))
            state.consecutive_short_count = 0
            state.last_signal_dt = datetime.now(UTC)
            state.trades_today += 1
            return ImbalanceSignal(
                side="SHORT", entry_price=round(entry, 4),
                stop=round(stop, 4), target=round(target, 4),
                confidence=round(confidence, 2),
                rationale=f"book imbalance {1.0/ratio:.2f}x ask:bid "
                          f"({ask_qty}:{bid_qty}) for {config.consecutive_snaps} snaps",
                snapshot_ts=str(snapshot.get("ts", "")),
            )
    else:
        # Reset both counters when ratio is in neutral zone
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
    return None


# ── Spread regime filter (Phase 4 partner) ────────────────────────


@dataclass
class SpreadRegimeConfig:
    """Tuning surface for the global spread-regime filter."""
    lookback_minutes: int = 20         # rolling window for median spread
    pause_at_multiple: float = 4.0     # pause all entries when spread > median * this
    resume_at_multiple: float = 2.0    # only resume once spread drops below median * this


@dataclass
class SpreadRegimeState:
    """Carried across snapshots."""
    recent_spreads: list[float] = field(default_factory=list)
    paused: bool = False
    paused_at: datetime | None = None


def update_spread_regime(snapshot: dict, config: SpreadRegimeConfig,
                          state: SpreadRegimeState) -> dict:
    """Track rolling median spread; return regime status.

    Output:
        {"paused": bool, "current_spread": float, "median": float,
         "ratio": float, "verdict": "NORMAL"|"WIDE"|"PAUSE"}

    Strategies should refuse to enter when verdict='PAUSE'.
    """
    spread = float(snapshot.get("spread", 0.0))
    state.recent_spreads.append(spread)
    # Cap at lookback*60 snaps (depth is 1Hz)
    max_len = config.lookback_minutes * 60
    if len(state.recent_spreads) > max_len:
        state.recent_spreads = state.recent_spreads[-max_len:]

    if not state.recent_spreads:
        return {"paused": False, "current_spread": spread, "median": 0.0,
                "ratio": 0.0, "verdict": "NORMAL"}

    sorted_spreads = sorted(state.recent_spreads)
    median = sorted_spreads[len(sorted_spreads) // 2]

    if median <= 0:
        return {"paused": False, "current_spread": spread, "median": median,
                "ratio": 0.0, "verdict": "NORMAL"}

    ratio = spread / median

    # Hysteresis: pause at higher threshold, resume at lower
    if state.paused:
        if ratio <= config.resume_at_multiple:
            state.paused = False
            state.paused_at = None
            verdict = "NORMAL"
        else:
            verdict = "PAUSE"
    else:
        if ratio >= config.pause_at_multiple:
            state.paused = True
            state.paused_at = datetime.now(UTC)
            verdict = "PAUSE"
        elif ratio >= config.resume_at_multiple:
            verdict = "WIDE"
        else:
            verdict = "NORMAL"

    return {"paused": state.paused, "current_spread": round(spread, 4),
            "median": round(median, 4), "ratio": round(ratio, 2),
            "verdict": verdict}


# ── Public API ────────────────────────────────────────────────────


def make_book_imbalance_strategy(config: BookImbalanceConfig | None = None) -> Any:
    """Factory mirror of how the registry-strategy bridge constructs
    other strategies.  Returns an object with ``evaluate(snapshot, atr)``."""
    cfg = config or BookImbalanceConfig()
    state = BookImbalanceState()

    class _BookImbalanceStrategy:
        def __init__(self) -> None:
            self.cfg = cfg
            self.state = state

        def evaluate(self, snapshot: dict, atr: float = 1.0) -> ImbalanceSignal | None:
            return evaluate_snapshot(snapshot, self.cfg, self.state, atr=atr)

    return _BookImbalanceStrategy()


def make_spread_regime_filter(config: SpreadRegimeConfig | None = None) -> Any:
    cfg = config or SpreadRegimeConfig()
    state = SpreadRegimeState()

    class _SpreadRegimeFilter:
        def __init__(self) -> None:
            self.cfg = cfg
            self.state = state

        def update(self, snapshot: dict) -> dict:
            return update_spread_regime(snapshot, self.cfg, self.state)

    return _SpreadRegimeFilter()
