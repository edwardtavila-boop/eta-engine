"""
EVOLUTIONARY TRADING ALGO  //  strategies.microprice_drift_strategy
====================================================================
Phase-4 L2 strategy: trade dislocations between the microprice
(qty-weighted mid of NBBO) and the trade-print price.

Why this exists
---------------
The microprice is a well-known order-flow predictor (Stoikov 2018):
    microprice = (best_bid * ask_qty + best_ask * bid_qty)
                 / (bid_qty + ask_qty)

It weights mid by the OPPOSITE side's qty, capturing that price
"wants to" move toward the side with thinner liquidity.  When trade
prints lag the microprice, there's a momentary edge:
- microprice > mid → market wants to go up; if last print is at mid
  or below, take long
- microprice < mid → market wants to go down; if last print is at
  mid or above, take short

Mechanic
--------
1. Track microprice on each depth snapshot.
2. Detect "drift" when microprice diverges from last_trade_price by
   more than drift_threshold_ticks for K consecutive snapshots.
3. Enter in the direction of the drift.
4. Confirmation gates:
   - spread filter (don't trade in WIDE/PAUSE regimes)
   - drift must be sustained, not a 1-snap spike
5. Stop: 1.5×ATR (tighter than book_imbalance — drifts are fast
   reversion plays).  Target: 2×ATR.

Storage
-------
Read-only — consumes depth snapshots (for microprice) and tick
stream (for last_trade_price).

Limitations
-----------
- Microprice is a momentum signal in the very short horizon
  (seconds to minutes), so longer holding periods are risky.
- Strategy state machine needs BOTH depth + ticks; degrades to
  no-op when one is missing.
"""

from __future__ import annotations

# ruff: noqa: ANN401
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class MicropriceConfig:
    """Tuning surface."""

    drift_threshold_ticks: float = 2.0  # min microprice-vs-trade divergence
    consecutive_snaps: int = 3  # min consecutive snaps above threshold
    atr_stop_mult: float = 1.5
    rr_target: float = 2.0
    min_stop_ticks: int = 4
    max_trades_per_day: int = 6
    cooldown_seconds: float = 60.0
    snapshot_interval_seconds: float = 5.0
    gap_reset_multiple: float = 2.0
    tick_size: float = 0.25
    n_levels: int = 1  # microprice uses level-1 only by definition


@dataclass
class MicropriceState:
    consecutive_long_count: int = 0
    consecutive_short_count: int = 0
    last_signal_dt: datetime | None = None
    last_snapshot_dt: datetime | None = None
    trades_today: int = 0
    today_str: str = ""
    last_trade_price: float | None = None
    last_microprice: float | None = None
    emitted_signal_ids: set[str] = field(default_factory=set)
    recent_drifts: deque[float] = field(default_factory=lambda: deque(maxlen=20))


@dataclass
class MicropriceSignal:
    side: str
    entry_price: float
    stop: float
    target: float
    confidence: float
    rationale: str
    snapshot_ts: str
    signal_id: str
    qty_contracts: int
    symbol: str
    microprice: float
    trade_price: float
    drift_ticks: float


def compute_microprice(snapshot: dict) -> tuple[float | None, float | None, str]:
    """Return (microprice, mid, classification).  classification ∈
    {OK, EMPTY_BIDS, EMPTY_ASKS, BOTH_EMPTY}.

    microprice = (best_bid * ask_qty + best_ask * bid_qty) / (bid_qty + ask_qty)
    """
    bids = snapshot.get("bids", [])
    asks = snapshot.get("asks", [])
    if not bids and not asks:
        return None, None, "BOTH_EMPTY"
    if not bids:
        return None, None, "EMPTY_BIDS"
    if not asks:
        return None, None, "EMPTY_ASKS"
    # Defensive None coercion: real depth feeds may publish bid/ask
    # entries with price=None or size=None during momentary book
    # crossings or auction transitions.  Coerce with ``or 0`` so
    # the downstream "total_qty <= 0 -> BOTH_EMPTY" guard catches
    # malformed level-1 entries instead of crashing float().
    best_bid = float(bids[0].get("price") or 0)
    best_ask = float(asks[0].get("price") or 0)
    bid_qty = float(bids[0].get("size") or 0)
    ask_qty = float(asks[0].get("size") or 0)
    if best_bid <= 0 and best_ask <= 0:
        return None, None, "BOTH_EMPTY"
    if best_bid <= 0:
        return None, None, "EMPTY_BIDS"
    if best_ask <= 0:
        return None, None, "EMPTY_ASKS"
    if bid_qty <= 0 and ask_qty <= 0:
        return None, None, "BOTH_EMPTY"
    if bid_qty <= 0:
        return None, None, "EMPTY_BIDS"
    if ask_qty <= 0:
        return None, None, "EMPTY_ASKS"
    total_qty = bid_qty + ask_qty
    micro = (best_bid * ask_qty + best_ask * bid_qty) / total_qty
    mid = (best_bid + best_ask) / 2
    return micro, mid, "OK"


def _snapshot_dt(snapshot: dict) -> datetime | None:
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


def update_trade_price(state: MicropriceState, price: float, ts: datetime | None = None) -> None:
    """Caller wires this from the tick stream — each new trade print."""
    _ = ts  # accepted for future use (e.g., trade-price latency)
    state.last_trade_price = price


def evaluate_snapshot(
    snapshot: dict, config: MicropriceConfig, state: MicropriceState, *, atr: float = 1.0, symbol: str = "MNQ"
) -> MicropriceSignal | None:
    """Process one depth snapshot.  Updates state.  Returns signal or None."""
    snap_dt = _snapshot_dt(snapshot)
    today = (snap_dt or datetime.now(UTC)).strftime("%Y%m%d")
    if state.today_str != today:
        state.today_str = today
        state.trades_today = 0
        state.emitted_signal_ids.clear()

    # Cooldown
    if (
        state.last_signal_dt is not None
        and snap_dt is not None
        and (snap_dt - state.last_signal_dt).total_seconds() < config.cooldown_seconds
    ):
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
        state.last_snapshot_dt = snap_dt
        return None

    if state.trades_today >= config.max_trades_per_day:
        return None

    # Gap-aware reset
    if state.last_snapshot_dt is not None and snap_dt is not None:
        gap = (snap_dt - state.last_snapshot_dt).total_seconds()
        if gap > config.gap_reset_multiple * config.snapshot_interval_seconds:
            state.consecutive_long_count = 0
            state.consecutive_short_count = 0
    state.last_snapshot_dt = snap_dt or state.last_snapshot_dt

    micro, mid, classification = compute_microprice(snapshot)
    if classification != "OK" or micro is None or mid is None:
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
        return None
    state.last_microprice = micro

    if state.last_trade_price is None:
        return None  # need at least one trade print

    # Compute drift in TICKS
    drift_price = micro - state.last_trade_price
    drift_ticks = drift_price / config.tick_size
    state.recent_drifts.append(drift_ticks)

    # Sanity: stop floor
    stop_distance = atr * config.atr_stop_mult
    min_stop_distance = config.min_stop_ticks * config.tick_size
    if stop_distance < min_stop_distance:
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
        return None

    threshold = config.drift_threshold_ticks
    if drift_ticks >= threshold:
        # Microprice ABOVE trade price → market wants up
        state.consecutive_long_count += 1
        state.consecutive_short_count = 0
        if state.consecutive_long_count >= config.consecutive_snaps:
            entry = mid + (snapshot.get("spread", 0.0) or 0.0) / 2
            stop = entry - stop_distance
            target = entry + stop_distance * config.rr_target
            state.consecutive_long_count = 0
            state.last_signal_dt = snap_dt or datetime.now(UTC)
            state.trades_today += 1
            return _emit(
                side="LONG",
                entry=entry,
                stop=stop,
                target=target,
                micro=micro,
                trade_price=state.last_trade_price,
                drift_ticks=drift_ticks,
                snapshot=snapshot,
                symbol=symbol,
                state=state,
                config=config,
            )
    elif drift_ticks <= -threshold:
        state.consecutive_short_count += 1
        state.consecutive_long_count = 0
        if state.consecutive_short_count >= config.consecutive_snaps:
            entry = mid - (snapshot.get("spread", 0.0) or 0.0) / 2
            stop = entry + stop_distance
            target = entry - stop_distance * config.rr_target
            state.consecutive_short_count = 0
            state.last_signal_dt = snap_dt or datetime.now(UTC)
            state.trades_today += 1
            return _emit(
                side="SHORT",
                entry=entry,
                stop=stop,
                target=target,
                micro=micro,
                trade_price=state.last_trade_price,
                drift_ticks=drift_ticks,
                snapshot=snapshot,
                symbol=symbol,
                state=state,
                config=config,
            )
    else:
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
    return None


def _emit(
    *,
    side: str,
    entry: float,
    stop: float,
    target: float,
    micro: float,
    trade_price: float,
    drift_ticks: float,
    snapshot: dict,
    symbol: str,
    state: MicropriceState,
    config: MicropriceConfig,
) -> MicropriceSignal:
    snapshot_ts = str(snapshot.get("ts", ""))
    signal_id = f"{symbol}-MICRO-{side}-{snapshot_ts}"
    state.emitted_signal_ids.add(signal_id)
    return MicropriceSignal(
        side=side,
        entry_price=round(entry, 4),
        stop=round(stop, 4),
        target=round(target, 4),
        confidence=round(min(1.0, abs(drift_ticks) / 10.0), 2),
        rationale=(
            f"microprice drift={drift_ticks:+.2f} ticks "
            f"(micro={micro:.4f}, trade={trade_price:.4f}) "
            f"for {config.consecutive_snaps} snaps"
        ),
        snapshot_ts=snapshot_ts,
        signal_id=signal_id,
        qty_contracts=1,
        symbol=symbol,
        microprice=round(micro, 4),
        trade_price=round(trade_price, 4),
        drift_ticks=round(drift_ticks, 2),
    )


def make_microprice_strategy(config: MicropriceConfig | None = None, *, symbol: str = "MNQ") -> Any:
    cfg = config or MicropriceConfig()
    state = MicropriceState()

    class _MicropriceStrategy:
        def __init__(self) -> None:
            self.cfg = cfg
            self.state = state
            self.symbol = symbol

        def update_trade(self, price: float, ts: datetime | None = None) -> None:
            update_trade_price(self.state, price, ts)

        def evaluate(self, snapshot: dict, atr: float = 1.0) -> MicropriceSignal | None:
            return evaluate_snapshot(snapshot, self.cfg, self.state, atr=atr, symbol=self.symbol)

    return _MicropriceStrategy()
