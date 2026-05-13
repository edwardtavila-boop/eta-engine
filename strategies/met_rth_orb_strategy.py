"""
EVOLUTIONARY TRADING ALGO  //  strategies.met_rth_orb_strategy
================================================================
MET (CME Micro Ether Future) — 5-minute Opening Range Breakout
on CME RTH.

Concept
-------
Same mechanic as the canonical ORBStrategy used for MNQ — define
the opening range as the first 5 minutes of CME RTH, then enter
on a clean breakout in either direction. We re-implement here as
a SINGLE-PURPOSE class so MET-specific tick quantization, session
window, and conservative defaults stay self-contained and don't
leak parameters into the index-futures ORB tuning.

Mechanic
--------
1. First ``range_minutes`` of CME RTH (08:30-08:35 CT default)
   define the opening range.
2. After the range, watch for the first bar whose high breaks the
   range high (LONG) or whose low breaks the range low (SHORT).
3. Enter at the breakout bar's close. Stop = 1.0x ATR. Target =
   2.0R (~ 2x risk).
4. One trade per session. Flatten by 14:50 CT.

RTH gating is hard — outside the window the strategy returns None.

Risk
----
- 1.0x ATR stop (tighter than spot crypto's 1.5-2.0).
- Stop/target tick-quantized to MET's 0.50 USD tick.
- Single trade per RTH day; max_entry deadline at 11:00 CT.

Status
------
research_candidate — defaults are CONSERVATIVE. Walk-forward
validation must precede any promotion past paper-soak. See TODO
notes for explicit calibration items.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


_MET_TICK_SIZE: float = 0.50  # MET tick = 0.50 USD per CME spec
# CME Micro Ether: 0.10 ETH per contract. $1 of price move = $0.10 P&L.
# Sizing math MUST multiply stop_dist by this to compute correct contract count.
_MET_POINT_VALUE: float = 0.10


@dataclass
class _DayState:
    """Per-day state carried between bars."""

    date: object
    range_high: float | None = None
    range_low: float | None = None
    range_complete: bool = False
    breakout_taken: bool = False
    trades_today: int = 0


@dataclass(frozen=True)
class METRTHORBConfig:
    """Parameters for the MET RTH 5m ORB.

    Defaults are CONSERVATIVE — chosen for sanity over performance.
    Walk-forward optimization is the next gate.
    """

    # Range definition — first 5m of RTH for a 5m bar window.
    # TODO(walk-forward): test 5m vs 15m range against MET data.
    range_minutes: int = 5
    rth_open_local: time = time(8, 30)
    rth_close_local: time = time(15, 0)
    timezone_name: str = "America/Chicago"

    # Trade window
    max_entry_local: time = time(11, 0)
    flatten_at_local: time = time(14, 50)

    # Entry filters
    # Minimum opening-range width in MET points. MET trades around
    # $2k-5k notional; 1.0 USD = 2 ticks. Skips dead-tape opens.
    # TODO(walk-forward): tune this against MET ATR distribution.
    min_range_pts: float = 1.0
    ema_bias_period: int = 0  # 0 = disabled by default
    volume_mult: float = 0.0  # 0 = disabled
    volume_lookback: int = 20

    # Risk / sizing
    atr_period: int = 14
    atr_stop_mult: float = 1.0
    rr_target: float = 2.0
    risk_per_trade_pct: float = 0.005
    max_trades_per_day: int = 1


class METRTHORBStrategy:
    """Single-purpose 5m ORB for MET on CME RTH.

    Stateful: tracks today's opening range, EMA bias, and breakout
    flag. The engine instantiates one instance per backtest run.
    """

    def __init__(self, config: METRTHORBConfig | None = None) -> None:
        self.cfg = config or METRTHORBConfig()
        self._tz = ZoneInfo(self.cfg.timezone_name)
        self._day: _DayState | None = None
        self._ema: float | None = None
        self._ema_alpha = 2.0 / (self.cfg.ema_bias_period + 1) if self.cfg.ema_bias_period > 0 else 0.0
        # Audit
        self._n_breakouts_seen: int = 0
        self._n_min_range_rejects: int = 0
        self._n_volume_rejects: int = 0
        self._n_fired: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "breakouts_seen": self._n_breakouts_seen,
            "min_range_rejects": self._n_min_range_rejects,
            "volume_rejects": self._n_volume_rejects,
            "entries_fired": self._n_fired,
        }

    # -- helpers ----------------------------------------------------------

    def _local_time(self, bar: BarData) -> time:
        local_t = bar.timestamp.astimezone(self._tz).timetz()
        return time(local_t.hour, local_t.minute, local_t.second)

    @staticmethod
    def _quantize_to_tick(price: float, tick: float) -> float:
        if tick <= 0.0:
            return price
        return round(price / tick) * tick

    @staticmethod
    def _atr(hist: list[BarData], period: int) -> float:
        if not hist:
            return 0.0
        window = hist[-period:]
        if not window:
            return 0.0
        return sum(b.high - b.low for b in window) / len(window)

    @staticmethod
    def _add_minutes(t: time, minutes: int) -> time:
        total_minutes = t.hour * 60 + t.minute + minutes
        total_minutes %= 24 * 60
        return time(total_minutes // 60, total_minutes % 60)

    # -- main entry point ------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        local_t = self._local_time(bar)
        today = bar.timestamp.astimezone(self._tz).date()

        # Per-day state init/reset
        if self._day is None or self._day.date != today:
            self._day = _DayState(date=today)

        # Update EMA bias
        if self.cfg.ema_bias_period > 0:
            if self._ema is None:
                self._ema = bar.close
            else:
                self._ema = self._ema_alpha * bar.close + (1 - self._ema_alpha) * self._ema

        # Phase 1: build opening range
        if not self._day.range_complete:
            if local_t < self.cfg.rth_open_local:
                return None
            range_end = self._add_minutes(
                self.cfg.rth_open_local,
                self.cfg.range_minutes,
            )
            if local_t < range_end:
                self._day.range_high = bar.high if self._day.range_high is None else max(self._day.range_high, bar.high)
                self._day.range_low = bar.low if self._day.range_low is None else min(self._day.range_low, bar.low)
                return None
            self._day.range_complete = True

        if self._day.range_high is None or self._day.range_low is None:
            return None

        # Phase 2: gate entry window
        if self._day.breakout_taken:
            return None
        if self._day.trades_today >= self.cfg.max_trades_per_day:
            return None
        if local_t >= self.cfg.rth_close_local:
            return None
        if local_t >= self.cfg.max_entry_local:
            return None
        if local_t >= self.cfg.flatten_at_local:
            return None

        # ATR (needed for risk sizing)
        atr = self._atr(hist, self.cfg.atr_period)
        if atr <= 0.0:
            return None

        # Range-width filter
        range_width = self._day.range_high - self._day.range_low
        if self.cfg.min_range_pts > 0 and range_width < self.cfg.min_range_pts:
            self._n_min_range_rejects += 1
            return None

        # Detect breakout
        ema_value = self._ema if self.cfg.ema_bias_period > 0 else bar.close
        long_bias = ema_value is None or bar.close >= ema_value
        short_bias = ema_value is None or bar.close <= ema_value

        side: str | None = None
        if bar.high > self._day.range_high and long_bias:
            side = "BUY"
        elif bar.low < self._day.range_low and short_bias:
            side = "SELL"
        if side is None:
            return None

        self._n_breakouts_seen += 1

        # Volume confirmation (optional)
        if self.cfg.volume_mult > 0.0:
            recent = hist[-self.cfg.volume_lookback :] if hist else []
            avg_vol = sum(b.volume for b in recent) / len(recent) if recent else 0.0
            if avg_vol > 0.0 and bar.volume < self.cfg.volume_mult * avg_vol:
                self._n_volume_rejects += 1
                return None

        # Risk sizing
        stop_dist = self.cfg.atr_stop_mult * atr
        if stop_dist <= 0.0:
            return None
        risk_usd = equity * self.cfg.risk_per_trade_pct
        # qty = $risk / ($-per-contract for stop_dist of price)
        # MET point_value=0.10. Without the multiplier the strategy would
        # ask for fractional contracts that IBKR floors to 0 (no trade)
        # OR rounds up (overrisk). The fix lands the qty in the right OOM.
        qty = risk_usd / (stop_dist * _MET_POINT_VALUE)
        if qty <= 0.0:
            return None

        entry = bar.close
        if side == "BUY":
            raw_stop = entry - stop_dist
            raw_target = entry + self.cfg.rr_target * stop_dist
        else:
            raw_stop = entry + stop_dist
            raw_target = entry - self.cfg.rr_target * stop_dist

        stop = self._quantize_to_tick(raw_stop, _MET_TICK_SIZE)
        target = self._quantize_to_tick(raw_target, _MET_TICK_SIZE)
        # Tick-quantization safety
        if side == "BUY":
            if stop >= entry:
                stop = entry - _MET_TICK_SIZE
            if target <= entry:
                target = entry + _MET_TICK_SIZE
        else:
            if stop <= entry:
                stop = entry + _MET_TICK_SIZE
            if target >= entry:
                target = entry - _MET_TICK_SIZE

        from eta_engine.backtest.engine import _Open

        self._day.breakout_taken = True
        self._day.trades_today += 1
        self._n_fired += 1
        return _Open(
            entry_bar=bar,
            side=side,
            qty=qty,
            entry_price=entry,
            stop=stop,
            target=target,
            risk_usd=risk_usd,
            confluence=8.0,
            leverage=1.0,
            regime=f"met_rth_orb_{side.lower()}",
        )


# ---------------------------------------------------------------------------
# Preset
# ---------------------------------------------------------------------------


def met_rth_orb_preset() -> METRTHORBConfig:
    """Default research_candidate config for MET RTH 5m ORB.

    NOTE: defaults are CONSERVATIVE. Walk-forward validation
    against MET historical data is required before promotion.
    """
    return METRTHORBConfig(
        range_minutes=5,
        rth_open_local=time(8, 30),
        rth_close_local=time(15, 0),
        max_entry_local=time(11, 0),
        flatten_at_local=time(14, 50),
        timezone_name="America/Chicago",
        min_range_pts=1.0,
        ema_bias_period=0,
        volume_mult=0.0,
        atr_period=14,
        atr_stop_mult=1.0,
        rr_target=2.0,
        risk_per_trade_pct=0.005,
        max_trades_per_day=1,
    )
