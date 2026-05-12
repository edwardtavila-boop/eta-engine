"""
EVOLUTIONARY TRADING ALGO  //  strategies.aggressor_flow_strategy
=================================================================
Phase-4 L2 strategy: trade in the direction of sustained aggressor
imbalance, using the buy/sell-split bars produced by bar_builder_l1.

Why this exists
---------------
Per docs/IBKR_PRO_DATA_INVENTORY.md Phase 4:
> Bars now carry volume_buy + volume_sell + n_trades (after Phase 2
> bar reconstruction).  This unlocks a class of order-flow strategies
> that the legacy OHLCV-only bars couldn't support.

Mechanic
--------
1. Consume L1 bars (from bar_builder_l1.py) — each bar has
   volume_buy + volume_sell + volume_total.
2. Compute rolling aggressor imbalance ratio:
       ratio = (sum_buy - sum_sell) / sum_total
   over a window of N bars (default 10 = 50 min on 5m bars).
3. LONG signal: ratio >= threshold (e.g. +0.35 = 67.5/32.5 buy/sell)
   for K consecutive bars.
4. SHORT signal: ratio <= -threshold.
5. Confirmation: current bar must close in the direction of the
   imbalance (avoids fading sustained pressure).

Risk: ATR stop, RR target, max trades per day.

Storage
-------
Read-only — consumes mnq_data/history_l1/<SYMBOL>_<TF>_l1.csv as
emitted by ``bar_builder_l1.py``.

Tested with
-----------
Bars containing the schema:
    timestamp_utc, epoch_s, open, high, low, close,
    volume_total, volume_buy, volume_sell, n_trades
"""
from __future__ import annotations

# ruff: noqa: ANN401
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class AggressorFlowConfig:
    """Tuning surface."""
    window_bars: int = 10           # rolling lookback (in bars)
    entry_threshold: float = 0.35   # |ratio| min to fire
    consecutive_bars: int = 2       # min consecutive bars above threshold
    require_close_confirm: bool = True  # close must move in imbalance direction
    atr_stop_mult: float = 1.0
    rr_target: float = 2.0
    min_stop_ticks: int = 4
    max_trades_per_day: int = 6
    cooldown_seconds: float = 300.0
    tick_size: float = 0.25


@dataclass
class AggressorFlowState:
    bar_window: deque[dict] = field(default_factory=lambda: deque(maxlen=50))
    consecutive_long_count: int = 0
    consecutive_short_count: int = 0
    last_signal_dt: datetime | None = None
    trades_today: int = 0
    today_str: str = ""
    last_ratio: float = 0.0
    emitted_signal_ids: set[str] = field(default_factory=set)


@dataclass
class AggressorFlowSignal:
    side: str            # "LONG" | "SHORT"
    entry_price: float
    stop: float
    target: float
    confidence: float
    rationale: str
    bar_ts: str
    signal_id: str
    qty_contracts: int
    symbol: str
    imbalance_ratio: float
    consecutive_bars: int


def _bar_dt(bar: dict) -> datetime | None:
    """Parse bar timestamp from either timestamp_utc string or epoch_s float."""
    ts = bar.get("timestamp_utc")
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            pass
    epoch = bar.get("epoch_s")
    if isinstance(epoch, (int, float)):
        try:
            return datetime.fromtimestamp(float(epoch), UTC)
        except (OSError, OverflowError, ValueError):
            return None
    return None


def compute_imbalance_ratio(bars: list[dict]) -> tuple[float, float, float]:
    """Return (ratio, sum_buy, sum_sell) over the bar window.

    ratio = (buy - sell) / total.  Range [-1.0, +1.0].
    Returns (0, 0, 0) when total volume is zero (no signal).

    Defensive: bar_builder_l1 occasionally publishes bars where
    volume_buy or volume_sell is explicitly None (one side of the
    bar had zero qualifying prints).  ``dict.get(key, default)``
    returns the default ONLY when the key is absent, so a present-
    but-None value would crash float() — coerce with ``or 0.0``.
    """
    sum_buy = sum(float(b.get("volume_buy") or 0.0) for b in bars)
    sum_sell = sum(float(b.get("volume_sell") or 0.0) for b in bars)
    total = sum_buy + sum_sell
    if total <= 0:
        return 0.0, sum_buy, sum_sell
    return (sum_buy - sum_sell) / total, sum_buy, sum_sell


def evaluate_bar(bar: dict, config: AggressorFlowConfig,
                  state: AggressorFlowState, *,
                  atr: float = 1.0,
                  symbol: str = "MNQ") -> AggressorFlowSignal | None:
    """Process one bar.  Updates state.  Returns signal or None."""
    bar_dt = _bar_dt(bar)
    today = (bar_dt or datetime.now(UTC)).strftime("%Y%m%d")
    if state.today_str != today:
        state.today_str = today
        state.trades_today = 0
        state.emitted_signal_ids.clear()

    if state.trades_today >= config.max_trades_per_day:
        return None

    # Cooldown
    if (
        state.last_signal_dt is not None
        and bar_dt is not None
        and (bar_dt - state.last_signal_dt).total_seconds() < config.cooldown_seconds
    ):
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
        state.bar_window.append(bar)
        return None

    state.bar_window.append(bar)
    window = list(state.bar_window)[-config.window_bars:]
    if len(window) < config.window_bars:
        return None  # not enough history yet

    ratio, sum_buy, sum_sell = compute_imbalance_ratio(window)
    state.last_ratio = ratio
    if sum_buy + sum_sell <= 0:
        # Anomalous (no volume) — reset counters fail-closed
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
        return None

    # Sanity: stop floor
    stop_distance = atr * config.atr_stop_mult
    min_stop_distance = config.min_stop_ticks * config.tick_size
    if stop_distance < min_stop_distance:
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
        return None

    # Defensive None coercion — see compute_imbalance_ratio docstring.
    bar_close = float(bar.get("close") or 0.0)
    bar_open = float(bar.get("open") or 0.0)
    bar_direction = "UP" if bar_close > bar_open else ("DOWN" if bar_close < bar_open else "FLAT")

    # LONG side
    if ratio >= config.entry_threshold:
        state.consecutive_long_count += 1
        state.consecutive_short_count = 0
        confirmed = (not config.require_close_confirm) or bar_direction in {"UP", "FLAT"}
        if state.consecutive_long_count >= config.consecutive_bars and confirmed:
            entry = bar_close
            stop = entry - stop_distance
            target = entry + stop_distance * config.rr_target
            state.consecutive_long_count = 0
            state.last_signal_dt = bar_dt
            state.trades_today += 1
            return _emit(side="LONG", entry=entry, stop=stop, target=target,
                          ratio=ratio, n_consec=config.consecutive_bars,
                          bar=bar, symbol=symbol, state=state, config=config,
                          sum_buy=sum_buy, sum_sell=sum_sell)
    # SHORT side
    elif ratio <= -config.entry_threshold:
        state.consecutive_short_count += 1
        state.consecutive_long_count = 0
        confirmed = (not config.require_close_confirm) or bar_direction in {"DOWN", "FLAT"}
        if state.consecutive_short_count >= config.consecutive_bars and confirmed:
            entry = bar_close
            stop = entry + stop_distance
            target = entry - stop_distance * config.rr_target
            state.consecutive_short_count = 0
            state.last_signal_dt = bar_dt
            state.trades_today += 1
            return _emit(side="SHORT", entry=entry, stop=stop, target=target,
                          ratio=ratio, n_consec=config.consecutive_bars,
                          bar=bar, symbol=symbol, state=state, config=config,
                          sum_buy=sum_buy, sum_sell=sum_sell)
    else:
        # Neutral zone — reset both
        state.consecutive_long_count = 0
        state.consecutive_short_count = 0
    return None


def _emit(*, side: str, entry: float, stop: float, target: float,
          ratio: float, n_consec: int, bar: dict, symbol: str,
          state: AggressorFlowState, config: AggressorFlowConfig,
          sum_buy: float, sum_sell: float) -> AggressorFlowSignal:
    bar_ts = str(bar.get("timestamp_utc", ""))
    signal_id = f"{symbol}-AGGFLOW-{side}-{bar_ts}"
    state.emitted_signal_ids.add(signal_id)
    return AggressorFlowSignal(
        side=side,
        entry_price=round(entry, 4),
        stop=round(stop, 4),
        target=round(target, 4),
        confidence=round(min(1.0, abs(ratio) / 0.5), 2),
        rationale=(f"aggressor flow ratio={ratio:+.2f} (buy={sum_buy:.0f}, "
                   f"sell={sum_sell:.0f}) for {n_consec} consec bars"),
        bar_ts=bar_ts,
        signal_id=signal_id,
        qty_contracts=1,
        symbol=symbol,
        imbalance_ratio=round(ratio, 4),
        consecutive_bars=n_consec,
    )


def make_aggressor_flow_strategy(config: AggressorFlowConfig | None = None,
                                    *, symbol: str = "MNQ") -> Any:
    cfg = config or AggressorFlowConfig()
    state = AggressorFlowState()

    class _AggressorFlowStrategy:
        def __init__(self) -> None:
            self.cfg = cfg
            self.state = state
            self.symbol = symbol

        def evaluate(self, bar: dict, atr: float = 1.0) -> AggressorFlowSignal | None:
            return evaluate_bar(bar, self.cfg, self.state, atr=atr, symbol=self.symbol)

    return _AggressorFlowStrategy()
