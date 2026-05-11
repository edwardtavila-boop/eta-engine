"""
EVOLUTIONARY TRADING ALGO  //  strategies.footprint_absorption_strategy
=======================================================================
Phase-4 L2 strategy: detect large aggressor prints that get absorbed
by hidden liquidity — a tell that smart money is soaking aggression
and anticipates continuation in the SAME direction as the aggression.

Why this exists
---------------
Per docs/IBKR_PRO_DATA_INVENTORY.md Phase 4 wishlist:
> Beyond book_imbalance, we want strategies that exploit
> microstructure patterns invisible in bars: footprint absorption,
> aggressor flow imbalance, microprice drift.

Mechanic
--------
1. Track a rolling window of recent trade prints (size, side, price)
   and the corresponding depth snapshots (visible top-of-book qty).
2. ABSORPTION SIGNAL fires when:
     - A "large" aggressor print arrives (size > N× rolling mean,
       configurable via prints_size_z_min)
     - The visible top-of-book qty on the OPPOSITE side did NOT
       drop by anywhere close to the print size (absorption_ratio
       threshold)
     - Price did not move significantly (within absorb_price_band
       ticks of pre-print mid)
3. Interpretation: a large sell print hit the bid but bids didn't
   thin out → hidden buyers absorbed → expect continuation UP.
   Mirror for buy print absorbed by hidden sellers → continuation DOWN.
4. Entry: in the direction of the absorption (LONG on absorbed sell,
   SHORT on absorbed buy), at NBBO mid.
5. Stop: 1×ATR. Target: 2×ATR (RR 2:1).

Storage
-------
Read-only — consumes ticks + depth snapshots from existing capture
files.  Does not write trade data.

Limitations
-----------
- Requires BOTH tick stream AND depth snapshots present and
  correlated; weak signal when capture is sparse.
- "Hidden liquidity" is inferred from visible-qty stability, not
  directly observed (impossible without exchange-level iceberg data).
"""
from __future__ import annotations

# ruff: noqa: ANN401
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class FootprintAbsorptionConfig:
    """Tuning surface."""
    # Print size threshold — print must exceed this z-score above
    # the rolling mean of recent print sizes to qualify as "large"
    prints_size_z_min: float = 1.5
    prints_lookback: int = 50  # how many recent prints to use for z-score
    # Absorption confirmation — opposite-side visible qty (top-of-book)
    # must NOT have dropped by more than this fraction of the print size
    # (e.g. 0.5 = book dropped <= 50% of print size → absorbed)
    absorption_ratio: float = 0.5
    # Price band — pre-print mid vs post-print mid must be within
    # absorb_price_band * tick to confirm absorption (real absorption
    # doesn't move price; if price moved a lot, it wasn't absorbed,
    # the buyers/sellers just walked the book)
    absorb_price_band_ticks: float = 2.0
    # Risk
    atr_stop_mult: float = 1.0
    rr_target: float = 2.0
    min_stop_ticks: int = 4
    # Hygiene
    max_trades_per_day: int = 6
    cooldown_seconds: float = 120.0  # 2-min cooldown between fires
    # Multi-symbol
    tick_size: float = 0.25  # MNQ default; override per symbol


@dataclass
class FootprintAbsorptionState:
    """Carried across ticks + snaps."""
    recent_prints: deque[dict] = field(default_factory=lambda: deque(maxlen=200))
    last_signal_dt: datetime | None = None
    trades_today: int = 0
    today_str: str = ""
    emitted_signal_ids: set[str] = field(default_factory=set)


@dataclass
class FootprintSignal:
    side: str            # "LONG" | "SHORT"
    entry_price: float
    stop: float
    target: float
    confidence: float
    rationale: str
    snapshot_ts: str
    signal_id: str
    qty_contracts: int
    symbol: str
    # Diagnostic fields
    print_size: float
    print_z_score: float
    opposite_qty_drop_pct: float


def record_print(state: FootprintAbsorptionState, *, price: float, size: float,
                  side: str, ts: datetime, mid_before: float, mid_after: float,
                  opposite_qty_before: int, opposite_qty_after: int) -> None:
    """Caller wires this from the tick stream + a depth snapshot
    pair (one just before the print, one just after).

    side is the aggressor side: BUY = trade printed on the ask (lifted),
    SELL = trade printed on the bid (hit).
    """
    state.recent_prints.append({
        "ts": ts,
        "price": price,
        "size": size,
        "side": side.upper(),
        "mid_before": mid_before,
        "mid_after": mid_after,
        "opposite_qty_before": opposite_qty_before,
        "opposite_qty_after": opposite_qty_after,
    })


def _z_score(value: float, series: list[float]) -> float:
    """Standard z-score of value vs series.  Returns 0 if series too small."""
    if len(series) < 10:
        return 0.0
    mean = sum(series) / len(series)
    var = sum((x - mean) ** 2 for x in series) / max(len(series) - 1, 1)
    std = var ** 0.5
    if std <= 0:
        return 0.0
    return (value - mean) / std


def evaluate_footprint(state: FootprintAbsorptionState,
                        config: FootprintAbsorptionConfig,
                        *, atr: float = 1.0,
                        symbol: str = "MNQ") -> FootprintSignal | None:
    """Evaluate the most recent print for absorption pattern.

    Caller should invoke this AFTER calling record_print() with the
    latest (print, pre-snapshot, post-snapshot) tuple.

    Pure function over (state, config) — easy to unit-test."""
    if not state.recent_prints:
        return None

    today = datetime.now(UTC).strftime("%Y%m%d")
    if state.today_str != today:
        state.today_str = today
        state.trades_today = 0
        state.emitted_signal_ids.clear()

    if state.trades_today >= config.max_trades_per_day:
        return None

    latest = state.recent_prints[-1]
    if state.last_signal_dt is not None:
        gap = (latest["ts"] - state.last_signal_dt).total_seconds()
        if gap < config.cooldown_seconds:
            return None

    # Compute z-score of latest print's size vs prior prints
    history_sizes = [p["size"] for p in list(state.recent_prints)[-config.prints_lookback:-1]]
    z = _z_score(latest["size"], history_sizes)
    if z < config.prints_size_z_min:
        return None

    # Check absorption: opposite-side visible qty did NOT drop much
    qty_before = max(latest["opposite_qty_before"], 1)
    qty_drop = qty_before - latest["opposite_qty_after"]
    drop_fraction = qty_drop / max(latest["size"], 1)
    if drop_fraction > config.absorption_ratio:
        # Book DID drop — not absorbed, just aggressed normally
        return None

    # Check price band: real absorption doesn't move price
    price_move = abs(latest["mid_after"] - latest["mid_before"])
    band = config.absorb_price_band_ticks * config.tick_size
    if price_move > band:
        return None

    # Sanity: stop floor (B6 alignment)
    stop_distance = atr * config.atr_stop_mult
    min_stop_distance = config.min_stop_ticks * config.tick_size
    if stop_distance < min_stop_distance:
        return None

    # Build signal — direction is OPPOSITE of aggressor (absorbed by hidden)
    mid = latest["mid_after"]
    aggressor = latest["side"]
    if aggressor == "BUY":
        # Buy was absorbed by hidden seller → expect down move
        side = "SHORT"
        entry = mid
        stop = entry + stop_distance
        target = entry - stop_distance * config.rr_target
    else:
        # Sell absorbed by hidden buyer → expect up move
        side = "LONG"
        entry = mid
        stop = entry - stop_distance
        target = entry + stop_distance * config.rr_target

    state.last_signal_dt = latest["ts"]
    state.trades_today += 1
    snapshot_ts = latest["ts"].isoformat() if isinstance(latest["ts"], datetime) else str(latest["ts"])
    signal_id = f"{symbol}-FOOTPRINT-{side}-{snapshot_ts}"
    state.emitted_signal_ids.add(signal_id)
    return FootprintSignal(
        side=side,
        entry_price=round(entry, 4),
        stop=round(stop, 4),
        target=round(target, 4),
        confidence=round(min(1.0, z / 5.0), 2),  # confidence scales with z-score
        rationale=(f"footprint absorption: {aggressor}-print size={latest['size']:.0f} "
                   f"(z={z:.2f}) absorbed (opp_qty_drop={drop_fraction*100:.0f}% < "
                   f"{config.absorption_ratio*100:.0f}%, "
                   f"price_move={price_move:.4f} <= {band:.4f})"),
        snapshot_ts=snapshot_ts,
        signal_id=signal_id,
        qty_contracts=1,  # hard-capped paper default
        symbol=symbol,
        print_size=latest["size"],
        print_z_score=round(z, 2),
        opposite_qty_drop_pct=round(drop_fraction * 100, 1),
    )


def make_footprint_strategy(config: FootprintAbsorptionConfig | None = None,
                              *, symbol: str = "MNQ") -> Any:
    """Factory mirror of the registry pattern."""
    cfg = config or FootprintAbsorptionConfig()
    state = FootprintAbsorptionState()

    class _FootprintStrategy:
        def __init__(self) -> None:
            self.cfg = cfg
            self.state = state
            self.symbol = symbol

        def record_print(self, **kw: Any) -> None:
            record_print(self.state, **kw)

        def evaluate(self, atr: float = 1.0) -> FootprintSignal | None:
            return evaluate_footprint(self.state, self.cfg, atr=atr, symbol=self.symbol)

    return _FootprintStrategy()
