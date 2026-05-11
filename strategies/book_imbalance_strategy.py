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

Spread regime filter
--------------------
The spread_regime classes + ``update_spread_regime`` /
``make_spread_regime_filter`` symbols are RE-EXPORTED from
``eta_engine.strategies.spread_regime_filter`` for backwards
compatibility with code/tests that import them via this module.
The canonical implementation lives in spread_regime_filter.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Backwards-compat re-export so tests/code that import these names from
# this module keep working.  Canonical home is spread_regime_filter.py.
from eta_engine.strategies.spread_regime_filter import (
    SpreadRegimeConfig,
    SpreadRegimeState,
    make_spread_regime_filter,
    update_spread_regime,
)
from eta_engine.strategies.spread_regime_filter import (
    check_staleness as check_spread_regime_staleness,
)

__all__ = [
    "BookImbalanceConfig",
    "BookImbalanceState",
    "ImbalanceSignal",
    "SpreadRegimeConfig",
    "SpreadRegimeState",
    "check_spread_regime_staleness",
    "compute_imbalance",
    "evaluate_snapshot",
    "get_tick_size",
    "make_book_imbalance_strategy",
    "make_spread_regime_filter",
    "update_spread_regime",
]

ROOT = Path(__file__).resolve().parents[1]


# Symbol → tick size lookup.  Used to translate spread thresholds
# (in TICKS) to absolute prices.  Add new symbols here as the
# multi-instrument fleet grows.  All values verified vs CME / NYMEX
# / COMEX product specs as of 2026-05.
TICK_SIZE_BY_SYMBOL: dict[str, float] = {
    "MNQ":  0.25,    # CME Micro E-mini Nasdaq-100
    "NQ":   0.25,    # CME E-mini Nasdaq-100
    "MES":  0.25,    # CME Micro E-mini S&P 500
    "ES":   0.25,    # CME E-mini S&P 500
    "MGC":  0.10,    # COMEX Micro Gold
    "GC":   0.10,    # COMEX Gold
    "MCL":  0.01,    # NYMEX Micro Crude Oil
    "CL":   0.01,    # NYMEX Crude Oil
    "M6E":  0.0001,  # CME Micro Euro FX
    "6E":   0.00005, # CME Euro FX
    "6B":   0.0001,  # CME British Pound
    "6J":   0.0000005,  # CME Japanese Yen
}


def get_tick_size(symbol: str) -> float:
    """Return tick size for symbol, raising on unknown so callers
    cannot silently use the wrong filter floor."""
    base = symbol.rstrip("1") if symbol.endswith("1") and len(symbol) > 1 else symbol
    if base not in TICK_SIZE_BY_SYMBOL:
        raise ValueError(
            f"Unknown tick size for symbol={symbol!r}. "
            f"Add it to TICK_SIZE_BY_SYMBOL in book_imbalance_strategy.py."
        )
    return TICK_SIZE_BY_SYMBOL[base]


@dataclass
class BookImbalanceConfig:
    """Tuning surface for book_imbalance entries."""
    n_levels: int = 3                  # depth-of-book rows per side
    entry_threshold: float = 1.75      # min bid/ask ratio for LONG (or inverse for SHORT)
    consecutive_snaps: int = 3         # snapshots required at threshold
    atr_stop_mult: float = 1.0         # stop = entry ± atr * mult
    rr_target: float = 2.0             # 2x stop distance to target
    cooldown_bars: int = 0             # optional re-entry cooldown in snap-count bars
    cooldown_seconds: float = 0.0      # alternative wall-clock cooldown; 0 = use cooldown_bars only
    max_trades_per_day: int = 6
    spread_min_ticks: float = 1.0      # don't enter when spread is at min (illiquid)
    spread_max_ticks: float = 3.0      # don't enter when spread is wide (regime change)
    min_stop_ticks: int = 4            # absolute floor on stop distance (in ticks); rejects atr too small
    snapshot_interval_seconds: float = 5.0  # depth capture cadence — used for gap detection + cooldown
    gap_reset_multiple: float = 2.0    # if snap arrives > N * cadence late, reset consecutive counters
    max_qty_contracts: int = 1         # absolute hard cap on emitted size; never derived from equity
    tick_size: float | None = None     # override symbol lookup; None → derive from signal_id symbol


@dataclass
class BookImbalanceState:
    """Per-bot state carried between bars."""
    consecutive_long_count: int = 0
    consecutive_short_count: int = 0
    last_signal_dt: datetime | None = None
    last_snapshot_dt: datetime | None = None  # gap-detection clock
    trades_today: int = 0
    today_str: str = ""
    last_snapshot: dict | None = None
    emitted_signal_ids: set[str] = field(default_factory=set)


@dataclass
class ImbalanceSignal:
    """What the strategy emits when an entry condition fires.

    The ``signal_id`` field is the deduplication key — downstream
    order routers MUST use it as the broker client-order-ID so two
    rapid-fire snapshots resolving to the same signal cannot place
    two orders.

    The ``qty_contracts`` field is the size to send to the broker.
    It is ALWAYS clamped to ``config.max_qty_contracts`` and is
    NEVER computed from equity / risk-per-trade math here — sizing
    discipline is enforced at signal generation, not in the router.
    """
    side: str            # "LONG" | "SHORT"
    entry_price: float
    stop: float
    target: float
    confidence: float    # 0.0 - 1.0 based on how far ratio exceeded threshold
    rationale: str       # one-line summary for the journal
    snapshot_ts: str     # ISO timestamp of the deciding snapshot
    signal_id: str       # idempotency key: f"{symbol}-{side}-{snapshot_ts}"
    qty_contracts: int   # hard-capped size; never derived from equity
    symbol: str          # the symbol this signal was emitted for


def _compute_imbalance_with_classification(
    snapshot: dict,
    n_levels: int,
) -> tuple[float, int, int, str]:
    """Return (ratio, bid_qty, ask_qty, classification) from top-N levels.

    classification:
        "OK"               — both sides have qty
        "EMPTY_BIDS"       — bid side empty (illiquid / halt / bad data)
        "EMPTY_ASKS"       — ask side empty
        "BOTH_EMPTY"       — both sides empty

    For empty-side cases, ratio is set to 1.0 (neutral) and caller
    should treat as fail-closed (no signal) rather than trade through
    the anomaly.  The classification field lets the caller distinguish
    "no signal" from "anomalous book — bail out hard"."""
    bids = snapshot.get("bids", [])[:n_levels]
    asks = snapshot.get("asks", [])[:n_levels]
    bid_qty = sum(int(lv.get("size", 0)) for lv in bids)
    ask_qty = sum(int(lv.get("size", 0)) for lv in asks)
    if bid_qty == 0 and ask_qty == 0:
        return 1.0, bid_qty, ask_qty, "BOTH_EMPTY"
    if bid_qty == 0:
        return 1.0, bid_qty, ask_qty, "EMPTY_BIDS"
    if ask_qty == 0:
        return 1.0, bid_qty, ask_qty, "EMPTY_ASKS"
    return bid_qty / ask_qty, bid_qty, ask_qty, "OK"


def compute_imbalance(snapshot: dict, n_levels: int) -> tuple[float, int, int]:
    """Return (ratio, bid_qty, ask_qty) from top-N levels.

    Keep the public API backward-compatible for existing strategy
    consumers.  Internal signal generation uses
    ``_compute_imbalance_with_classification`` when it needs the
    zero-side anomaly label.
    """
    ratio, bid_qty, ask_qty, _classification = _compute_imbalance_with_classification(
        snapshot,
        n_levels,
    )
    return ratio, bid_qty, ask_qty


def _snapshot_dt(snapshot: dict) -> datetime | None:
    """Best-effort parse of snapshot timestamp.  Returns None on failure."""
    ts = snapshot.get("ts")
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            pass
    epoch = snapshot.get("epoch_s")
    if isinstance(epoch, (int, float)):
        try:
            return datetime.fromtimestamp(float(epoch), UTC)
        except (OSError, OverflowError, ValueError):
            return None
    return None


def _within_cooldown(state: BookImbalanceState, config: BookImbalanceConfig,
                      snap_dt: datetime | None) -> bool:
    """Return True if the strategy is still cooling down from its last
    signal — caller refuses re-entry.

    Uses ``cooldown_seconds`` if > 0 (wall-clock guard), otherwise
    falls back to ``cooldown_bars * snapshot_interval_seconds`` (snap-count
    guard scaled to the configured cadence).  Defensive: if state has
    no last_signal_dt we are not in cooldown."""
    if state.last_signal_dt is None or snap_dt is None:
        return False
    if config.cooldown_seconds > 0:
        cooldown = config.cooldown_seconds
    else:
        cooldown = config.cooldown_bars * config.snapshot_interval_seconds
    if cooldown <= 0:
        return False
    elapsed = (snap_dt - state.last_signal_dt).total_seconds()
    return elapsed < cooldown


def _gap_too_large(state: BookImbalanceState, config: BookImbalanceConfig,
                   snap_dt: datetime | None) -> bool:
    """Return True if the gap from the last snapshot to this one is
    longer than ``config.gap_reset_multiple * snapshot_interval_seconds``.
    Caller treats this as "stale conviction → reset counters".  Without
    this guard, three snaps could span 30+ seconds (capture pause +
    resume) and the strategy would treat it as continuous conviction."""
    if state.last_snapshot_dt is None or snap_dt is None:
        return False
    gap_s = (snap_dt - state.last_snapshot_dt).total_seconds()
    threshold_s = config.gap_reset_multiple * config.snapshot_interval_seconds
    return gap_s > threshold_s


def evaluate_snapshot(snapshot: dict, config: BookImbalanceConfig,
                      state: BookImbalanceState, *,
                      atr: float = 1.0,
                      symbol: str = "MNQ") -> ImbalanceSignal | None:
    """Process one depth snapshot.  Updates `state`.  Returns a
    signal when the consecutive-snapshot threshold is hit, else None.

    Pure function over (snapshot, config, state, atr, symbol).

    Args:
        snapshot: depth snapshot dict (bids/asks/spread/mid/ts/epoch_s)
        config:   tuning
        state:    mutable strategy state
        atr:      current ATR in price points (NOT ticks)
        symbol:   symbol code, used to derive tick_size for the spread
                  filter and to build the idempotency key

    Returns ImbalanceSignal or None."""
    snap_dt = _snapshot_dt(snapshot)
    today = datetime.now(UTC).strftime("%Y%m%d")
    if state.today_str != today:
        state.today_str = today
        state.trades_today = 0
        state.emitted_signal_ids.clear()  # reset dedupe set per day

    # Cooldown gate (B3) — enforce cooldown_bars / cooldown_seconds
    if _within_cooldown(state, config, snap_dt):
        # Reset counters during cooldown so post-cooldown re-arming
        # requires fresh consecutive_snaps, not residual count.
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
        state.last_snapshot_dt = snap_dt or state.last_snapshot_dt
        return None

    if state.trades_today >= config.max_trades_per_day:
        return None

    # Gap-aware reset (I7) — if snap arrived too late, reset counters
    # so we don't claim "consecutive conviction" across a capture pause.
    if _gap_too_large(state, config, snap_dt):
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
    state.last_snapshot_dt = snap_dt or state.last_snapshot_dt

    # Spread filter (I5) — tick_size now per-symbol, not hardcoded MNQ
    tick = config.tick_size if config.tick_size is not None else get_tick_size(symbol)
    spread = snapshot.get("spread", 0.0)
    if spread < config.spread_min_ticks * tick:
        return None
    if spread > config.spread_max_ticks * tick:
        return None

    ratio, bid_qty, ask_qty, classification = _compute_imbalance_with_classification(
        snapshot,
        config.n_levels,
    )

    # I8: zero-side fail-closed.  When the book is anomalously thin
    # on one side (halt, illiquid, bad data) the ratio collapses to
    # the neutral sentinel — but we don't want to keep accumulating
    # counter from previous snaps and then fire on the first valid
    # snap after the anomaly.  Reset hard.
    if classification != "OK":
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
        state.last_snapshot = snapshot
        return None

    state.last_snapshot = snapshot
    mid = float(snapshot.get("mid", 0.0))

    # Stop-floor sanity (B6 prerequisite) — refuse to emit when ATR is
    # so small the stop collapses below min_stop_ticks * tick.
    stop_distance = atr * config.atr_stop_mult
    min_stop_distance = config.min_stop_ticks * tick
    if stop_distance < min_stop_distance:
        # Reset counters too — we don't want to fire on the first valid
        # ATR reading after a glitch with stale conviction.
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
        return None

    # LONG signal — bid side dominant for k snaps
    if ratio >= config.entry_threshold:
        state.consecutive_long_count += 1
        state.consecutive_short_count = 0
        if state.consecutive_long_count >= config.consecutive_snaps:
            entry = mid + spread / 2  # cross spread to take ask
            stop = entry - stop_distance
            target = entry + stop_distance * config.rr_target
            confidence = min(1.0, (ratio - config.entry_threshold) /
                              max(config.entry_threshold, 0.01))
            state.consecutive_long_count = 0
            state.last_signal_dt = snap_dt or datetime.now(UTC)
            state.trades_today += 1
            return _emit_signal(
                side="LONG", entry=entry, stop=stop, target=target,
                confidence=confidence, ratio=ratio,
                bid_qty=bid_qty, ask_qty=ask_qty,
                config=config, snapshot=snapshot,
                symbol=symbol, state=state,
            )
    # SHORT signal — ask side dominant
    elif ratio <= 1.0 / config.entry_threshold:
        state.consecutive_short_count += 1
        state.consecutive_long_count = 0
        if state.consecutive_short_count >= config.consecutive_snaps:
            entry = mid - spread / 2  # cross spread to take bid
            stop = entry + stop_distance
            target = entry - stop_distance * config.rr_target
            inv_ratio = 1.0 / max(ratio, 0.01)
            confidence = min(1.0, (inv_ratio - config.entry_threshold) /
                              max(config.entry_threshold, 0.01))
            state.consecutive_short_count = 0
            state.last_signal_dt = snap_dt or datetime.now(UTC)
            state.trades_today += 1
            return _emit_signal(
                side="SHORT", entry=entry, stop=stop, target=target,
                confidence=confidence, ratio=1.0 / ratio,
                bid_qty=ask_qty, ask_qty=bid_qty,  # swap for rationale
                config=config, snapshot=snapshot,
                symbol=symbol, state=state,
            )
    else:
        # Reset both counters when ratio is in neutral zone
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
    return None


def _emit_signal(*, side: str, entry: float, stop: float, target: float,
                  confidence: float, ratio: float,
                  bid_qty: int, ask_qty: int,
                  config: BookImbalanceConfig, snapshot: dict,
                  symbol: str,
                  state: BookImbalanceState) -> ImbalanceSignal:
    """Build the ImbalanceSignal with idempotency key + size cap.

    Idempotency: signal_id = f"{symbol}-{side}-{snapshot_ts}".  If
    this exact key was already emitted today, we still emit a new
    signal but tag it as a duplicate via ``signal_id`` collision —
    the broker order router is the dedup line.
    """
    snapshot_ts = str(snapshot.get("ts", ""))
    signal_id = f"{symbol}-{side}-{snapshot_ts}"
    state.emitted_signal_ids.add(signal_id)
    qty = max(1, min(config.max_qty_contracts, 1))  # paper-soak default
    label = "bid:ask" if side == "LONG" else "ask:bid"
    return ImbalanceSignal(
        side=side,
        entry_price=round(entry, 4),
        stop=round(stop, 4),
        target=round(target, 4),
        confidence=round(confidence, 2),
        rationale=f"book imbalance {ratio:.2f}x {label} "
                  f"({bid_qty}:{ask_qty}) for {config.consecutive_snaps} snaps",
        snapshot_ts=snapshot_ts,
        signal_id=signal_id,
        qty_contracts=qty,
        symbol=symbol,
    )


# ── Public API ────────────────────────────────────────────────────


def make_book_imbalance_strategy(
    config: BookImbalanceConfig | None = None,
    *,
    symbol: str = "MNQ",
) -> Any:  # noqa: ANN401 - strategy factory returns a duck-typed adapter.
    """Factory mirror of how the registry-strategy bridge constructs
    other strategies.  Returns an object with ``evaluate(snapshot, atr)``."""
    cfg = config or BookImbalanceConfig()
    state = BookImbalanceState()

    class _BookImbalanceStrategy:
        def __init__(self) -> None:
            self.cfg = cfg
            self.state = state
            self.symbol = symbol

        def evaluate(self, snapshot: dict, atr: float = 1.0) -> ImbalanceSignal | None:
            return evaluate_snapshot(snapshot, self.cfg, self.state,
                                     atr=atr, symbol=self.symbol)

    return _BookImbalanceStrategy()
