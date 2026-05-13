"""
EVOLUTIONARY TRADING ALGO  //  strategies.crypto_trend_strategy
================================================================
Crypto Trend-Following — EMA crossover + HTF bias + ATR trail.

Per the 2026-04-27 user directive on crypto bot strategies:

* "Trend Following / Break & Retest: Detect bias via higher-timeframe
  EMAs (4h or 1D 50/200). Enter on pullbacks to support/EMA or
  confirmed break + retest. Trail stops with ATR or swing points."

This strategy:

* **HTF bias** — gates direction on a slow EMA computed from the SAME
  bar stream (cheaper than streaming a second timeframe; on 1h bars an
  EMA(200) approximates a "1D-50" tilt for swing-style entries).
* **Fast/slow crossover** — EMA(9) crossing above EMA(21) is the long
  trigger; reverse for shorts. Crossovers fire ON the first bar where
  the cross becomes positive, so signals don't repeat.
* **ATR trail (target leg)** — fixed stop at entry minus 1.5×ATR; an
  RR-multiple target carries the rest of the framework's exit logic.
  We don't actively trail in the strategy itself — the backtest engine
  closes on stop / target / EOD just like every other ORB-family bot.

Why a separate strategy and not a parameter on Crypto-ORB
---------------------------------------------------------
Crypto-ORB anchors on a session boundary. Trend-following has no
session — it just rides whichever bar shows a fresh fast-over-slow
cross with HTF agreement. Different signal, different state, so a
separate file keeps each one independently tunable.

Returns
-------
``CryptoTrendStrategy.maybe_enter(bar, hist, equity, config)``
returns the same ``_Open | None`` shape the BacktestEngine consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CryptoTrendConfig:
    """All knobs in one place. Defaults tuned for BTC/1h."""

    # ── EMA crossover ──
    fast_ema: int = 9
    slow_ema: int = 21
    # ── HTF bias ──
    htf_ema: int = 200  # set to 0 to disable (cross alone)

    # ── Risk / exits ──
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    rr_target: float = 2.5  # crypto trends → wider target
    risk_per_trade_pct: float = 0.01

    # ── Entry hygiene ──
    # Minimum bars between consecutive entries (cooldown). Prevents the
    # same trend phase from firing twice if the fast EMA wobbles.
    min_bars_between_trades: int = 6
    max_trades_per_day: int = 3
    # Skip the first N bars while EMAs warm up. Below this, the EMA
    # value is not yet representative and crossovers are noisy.
    warmup_bars: int = 30


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class CryptoTrendStrategy:
    """EMA crossover + HTF bias trend-follower for 24/7 crypto bars.

    Stateful across the bar stream — tracks fast/slow/HTF EMAs and the
    last entry's bar index. The BacktestEngine instantiates one
    strategy per backtest run so cross-window state stays clean.
    """

    def __init__(self, config: CryptoTrendConfig | None = None) -> None:
        self.cfg = config or CryptoTrendConfig()
        self._fast: float | None = None
        self._slow: float | None = None
        self._htf: float | None = None
        self._fast_alpha = 2.0 / (self.cfg.fast_ema + 1)
        self._slow_alpha = 2.0 / (self.cfg.slow_ema + 1)
        self._htf_alpha = 2.0 / (self.cfg.htf_ema + 1) if self.cfg.htf_ema > 0 else 0.0
        self._prev_diff: float | None = None  # fast - slow on prior bar
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None

    # -- main entry point -----------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        """Return an _Open or None. Same engine contract as ORB."""
        # ── per-day reset for the daily trade-count latch ──
        bar_date = bar.timestamp.date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0

        self._bars_seen += 1

        # ── EMA updates ──
        if self._fast is None:
            self._fast = bar.close
        else:
            self._fast = self._fast_alpha * bar.close + (1 - self._fast_alpha) * self._fast
        if self._slow is None:
            self._slow = bar.close
        else:
            self._slow = self._slow_alpha * bar.close + (1 - self._slow_alpha) * self._slow
        if self.cfg.htf_ema > 0:
            if self._htf is None:
                self._htf = bar.close
            else:
                self._htf = self._htf_alpha * bar.close + (1 - self._htf_alpha) * self._htf

        # ── warmup gate ──
        if self._bars_seen < self.cfg.warmup_bars:
            self._prev_diff = self._fast - self._slow
            return None

        diff = self._fast - self._slow
        prev = self._prev_diff
        self._prev_diff = diff

        # No prior diff → can't detect a cross
        if prev is None:
            return None

        # ── per-trade latches ──
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx) < self.cfg.min_bars_between_trades
        ):
            return None

        # ── direction from cross ──
        side: str | None = None
        if prev <= 0.0 and diff > 0.0:
            side = "BUY"
        elif prev >= 0.0 and diff < 0.0:
            side = "SELL"
        if side is None:
            return None

        # ── HTF bias gate ──
        if self.cfg.htf_ema > 0 and self._htf is not None:
            if side == "BUY" and bar.close < self._htf:
                return None
            if side == "SELL" and bar.close > self._htf:
                return None

        # ── ATR sizing ──
        atr_window = hist[-self.cfg.atr_period :] if hist else []
        if len(atr_window) < 2:
            return None
        atr = sum(b.high - b.low for b in atr_window) / len(atr_window)
        if atr <= 0.0:
            return None
        stop_dist = self.cfg.atr_stop_mult * atr
        if stop_dist <= 0.0:
            return None
        risk_usd = equity * self.cfg.risk_per_trade_pct
        qty = risk_usd / stop_dist
        if qty <= 0.0:
            return None

        entry_price = bar.close
        if side == "BUY":
            stop = entry_price - stop_dist
            target = entry_price + self.cfg.rr_target * stop_dist
        else:
            stop = entry_price + stop_dist
            target = entry_price - self.cfg.rr_target * stop_dist

        from eta_engine.backtest.engine import _Open

        opened = _Open(
            entry_bar=bar,
            side=side,
            qty=qty,
            entry_price=entry_price,
            stop=stop,
            target=target,
            risk_usd=risk_usd,
            confluence=10.0,
            leverage=1.0,
            regime="crypto_trend",
        )
        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        return opened
