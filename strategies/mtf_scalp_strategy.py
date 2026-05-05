"""
EVOLUTIONARY TRADING ALGO  //  strategies.mtf_scalp_strategy
==============================================================
Multi-timeframe scalp: 15m direction + 1m micro-structure entry.

User mandate (2026-04-27): "being futures the strategy was
supposed to scalp on the 15 minute and find entry on 1min - micro
structure".

Mechanic
--------
The strategy runs on the LTF bar stream (1m bars) and consults
an HTF (15m) regime/direction read on every bar. The HTF read
gates direction; the LTF entry uses micro-structure (recent
low/high break, momentum, EMA pullback) for precise entry timing.

Two-layer flow:

  HTF (15m) — direction layer
    * Bias = sign of (close - 200_EMA_15m)
    * Volatility regime = ATR_14_15m / close
    * Active session: only fire during configured time window

  LTF (1m) — execution layer (only fires when HTF is active)
    * BUY trigger: bar.close > bar.open AND
                   bar.close > recent_high(N) AND
                   close > ema_fast_1m
    * SELL trigger: mirror

Stop / target sized off LTF ATR; cooldown enforced on LTF bars.

Why this is a scalper
---------------------
The 1m micro-entry is what makes it a scalper — entries are
fast, stops are tight (LTF ATR-based, typically 5-15 ticks on
MNQ), and the typical hold is 2-15 minutes. The 15m HTF gate
prevents fires during chop / wrong-direction tape.

Implementation note — single-stream operation
---------------------------------------------
The strategy operates on the 1m bar stream natively and
synthesizes 15m bars internally by accumulating every 15 1m
bars. This avoids the complexity of a separate HTF data feed
and keeps the engine contract simple (single ``maybe_enter``).

The synthesized 15m bar's open/high/low/close are computed from
the rolling 15-bar window of 1m bars; this matches the canonical
OHLCV resampling used by ``resample_btc_timeframes.py``.

Data requirement
----------------
Needs sufficient 1m history to populate:
* 15m EMA-200 (= 200 * 15 = 3000 1m bars warmup)
* 15m ATR-14 (= 14 * 15 = 210 1m bars)
* 1m fast EMA + recent-extreme window

As of 2026-04-27, MNQ1 1m has 22.7 days = ~6,400 RTH bars +
~16,000 ETH bars depending on filter — plenty for warmup but
thin for walk-forward validation. The strategy is built
for the data extension that's the gating need.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class MtfScalpConfig:
    """Knobs for the 15m direction + 1m entry scalper."""

    # HTF (15m) layer
    htf_bars_per_aggregate: int = 15  # 1m → 15m
    htf_ema_period: int = 100         # in HTF bars (EMA-100 captures ~95% of EMA-200 smoothing in half the time)
    htf_atr_period: int = 14          # in HTF bars
    # ATR percent of close: skip when too quiet (chop) or too loud (panic)
    htf_atr_pct_min: float = 0.05     # 0.05% of close
    htf_atr_pct_max: float = 0.50     # 0.50% of close

    # LTF (1m) layer
    ltf_recent_high_lookback: int = 5     # break recent N-bar high
    ltf_fast_ema_period: int = 9
    ltf_atr_period: int = 14
    ltf_atr_stop_mult: float = 1.5
    ltf_rr_target: float = 2.0
    risk_per_trade_pct: float = 0.005  # smaller risk per scalp

    # Hygiene
    min_bars_between_trades: int = 30   # 30 1m bars = 30 min cooldown
    max_trades_per_day: int = 6
    warmup_bars: int = 1500             # = htf_ema_period (100) * htf_bars_per_aggregate (15)

    # Session window — defaults to PERMISSIVE (full day) so 24/7 ticker
    # trading and Globex futures both work.  Operators may opt in to
    # RTH-only by setting rth_open_local=time(9,30), rth_close_local=time(15,55).
    rth_open_local: time = time(0, 0)
    rth_close_local: time = time(23, 59)
    timezone_name: str = "America/New_York"

    # Allow shorts? Default: both directions
    allow_long: bool = True
    allow_short: bool = True


def _ema_step(prev: float | None, value: float, period: int) -> float:
    if prev is None:
        return value
    alpha = 2.0 / (period + 1)
    return alpha * value + (1 - alpha) * prev


class MtfScalpStrategy:
    """15m direction + 1m micro-structure entry scalper."""

    def __init__(self, config: MtfScalpConfig | None = None) -> None:
        self.cfg = config or MtfScalpConfig()
        # HTF aggregation state
        self._htf_window: list[tuple[float, float, float, float]] = []  # (o,h,l,c)
        self._htf_ema: float | None = None
        self._htf_atr_window: deque[float] = deque(
            maxlen=self.cfg.htf_atr_period,
        )
        # LTF state
        self._ltf_ema: float | None = None
        # +1 because _ltf_recent_break does [:-1] to strip the current bar;
        # without the +1 we'd compare against only N-1 prior bars instead
        # of the configured N. The deque caps growth so memory is unaffected.
        self._ltf_recent_highs: deque[float] = deque(
            maxlen=self.cfg.ltf_recent_high_lookback + 1,
        )
        self._ltf_recent_lows: deque[float] = deque(
            maxlen=self.cfg.ltf_recent_high_lookback + 1,
        )
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None
        # Cached ZoneInfo for tz comparisons
        self._tz: ZoneInfo | None = None
        # Audit
        self._n_htf_active: int = 0
        self._n_ltf_triggered: int = 0
        self._n_session_blocks: int = 0
        self._n_vol_regime_blocks: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "htf_active": self._n_htf_active,
            "ltf_triggered": self._n_ltf_triggered,
            "session_blocks": self._n_session_blocks,
            "vol_regime_blocks": self._n_vol_regime_blocks,
        }

    def _get_tz(self) -> ZoneInfo:
        if self._tz is None:
            from zoneinfo import ZoneInfo
            self._tz = ZoneInfo(self.cfg.timezone_name)
        return self._tz

    def _in_session(self, bar: BarData) -> bool:
        local = bar.timestamp.astimezone(self._get_tz()).time()
        return self.cfg.rth_open_local <= local <= self.cfg.rth_close_local

    def _update_htf(self, bar: BarData) -> bool:
        """Append the LTF bar to the 15-bar accumulator. When the
        accumulator fills, compute a 15m bar and update HTF EMAs/ATR.
        Returns True iff the HTF state was updated this call.
        """
        n = self.cfg.htf_bars_per_aggregate
        idx_in_window = self._bars_seen % n
        # Track open / high / low / close for the synthesized HTF bar
        if idx_in_window == 0:
            self._htf_window = [(bar.open, bar.high, bar.low, bar.close)]
        else:
            self._htf_window.append(
                (bar.open, bar.high, bar.low, bar.close),
            )
        if (idx_in_window + 1) < n:
            return False
        # Window full — synthesize the 15m bar
        opens = [b[0] for b in self._htf_window]
        highs = [b[1] for b in self._htf_window]
        lows = [b[2] for b in self._htf_window]
        closes = [b[3] for b in self._htf_window]
        htf_open = opens[0]
        htf_high = max(highs)
        htf_low = min(lows)
        htf_close = closes[-1]
        self._htf_ema = _ema_step(
            self._htf_ema, htf_close, self.cfg.htf_ema_period,
        )
        self._htf_atr_window.append(htf_high - htf_low)
        # Avoid an unused-variable lint by referencing htf_open in the
        # audit state (downstream callers can access the synthesized bar)
        self._last_htf_open = htf_open
        return True

    def _ltf_recent_break(
        self, bar: BarData, side: str,
    ) -> bool:
        """Did this 1m bar break the recent N-bar high (long) or
        low (short)? Compares the close against the PRIOR window
        (excluding the current bar) so a green bar that closes above
        the recent N highs is a real break."""
        # Drop the current bar (last in deque) from the comparison
        prior_highs = list(self._ltf_recent_highs)[:-1]
        prior_lows = list(self._ltf_recent_lows)[:-1]
        if side == "BUY" and prior_highs:
            # bar broke the recent high if its high (or close) cleared
            # the prior-window max
            return bar.high > max(prior_highs) or bar.close >= max(prior_highs)
        if side == "SELL" and prior_lows:
            return bar.low < min(prior_lows) or bar.close <= min(prior_lows)
        return False

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        bar_date = bar.timestamp.date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0
        self._bars_seen += 1

        # Update LTF EMA + recent-extreme windows on every bar
        self._ltf_ema = _ema_step(
            self._ltf_ema, bar.close, self.cfg.ltf_fast_ema_period,
        )
        self._ltf_recent_highs.append(bar.high)
        self._ltf_recent_lows.append(bar.low)
        # Update HTF
        self._update_htf(bar)

        if self._bars_seen < self.cfg.warmup_bars:
            return None
        if self._htf_ema is None or len(self._htf_atr_window) < self.cfg.htf_atr_period:
            return None
        if self._ltf_ema is None:
            return None
        if not self._in_session(bar):
            self._n_session_blocks += 1
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx)
            < self.cfg.min_bars_between_trades
        ):
            return None

        # HTF direction read
        htf_atr = sum(self._htf_atr_window) / len(self._htf_atr_window)
        htf_atr_pct = (
            htf_atr / max(bar.close, 1e-9) * 100.0
        )
        if not (
            self.cfg.htf_atr_pct_min <= htf_atr_pct <= self.cfg.htf_atr_pct_max
        ):
            self._n_vol_regime_blocks += 1
            return None
        htf_bias = "long" if bar.close > self._htf_ema else "short"
        self._n_htf_active += 1

        # LTF entry — only allowed in HTF direction
        side: str | None = None
        if self.cfg.allow_long and htf_bias == "long":
            momentum = bar.close > bar.open
            above_ltf_ema = bar.close > self._ltf_ema
            broke_recent = self._ltf_recent_break(bar, "BUY")
            if momentum and above_ltf_ema and broke_recent:
                side = "BUY"
        if side is None and self.cfg.allow_short and htf_bias == "short":
            momentum = bar.close < bar.open
            below_ltf_ema = bar.close < self._ltf_ema
            broke_recent = self._ltf_recent_break(bar, "SELL")
            if momentum and below_ltf_ema and broke_recent:
                side = "SELL"
        if side is None:
            return None

        self._n_ltf_triggered += 1

        # Risk sizing off LTF ATR
        ltf_atr_window = hist[-self.cfg.ltf_atr_period:] if hist else []
        if len(ltf_atr_window) < 2:
            return None
        ltf_atr = sum(b.high - b.low for b in ltf_atr_window) / len(ltf_atr_window)
        if ltf_atr <= 0.0:
            return None
        stop_dist = self.cfg.ltf_atr_stop_mult * ltf_atr
        if stop_dist <= 0.0:
            return None
        risk_usd = equity * self.cfg.risk_per_trade_pct
        qty = risk_usd / stop_dist
        if qty <= 0.0:
            return None

        entry = bar.close
        if side == "BUY":
            stop = entry - stop_dist
            target = entry + self.cfg.ltf_rr_target * stop_dist
        else:
            stop = entry + stop_dist
            target = entry - self.cfg.ltf_rr_target * stop_dist

        from eta_engine.backtest.engine import _Open  # local import
        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        return _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=10.0, leverage=1.0,
            regime=f"mtf_scalp_htf_{htf_bias}_ltf_{side.lower()}",
        )
