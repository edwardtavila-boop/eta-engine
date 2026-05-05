"""
EVOLUTIONARY TRADING ALGO  //  strategies.crypto_regime_trend_strategy
=======================================================================
BTC 200-EMA regime + pullback-to-50 trend continuation.

User insight (2026-04-27): "BTC has more patterns showing and has
success when above the 200 EMA for bulls and below 200 EMA for bear
territory; it scopes in and out of timeframes — basically since it's
24/7 the past leads to the future, the patterns repeat."

Three rules:

* **Regime gate** — slow EMA (default 200) defines bull/bear regime.
  Only longs above, only shorts below.
* **Pullback entry** — bar.low taps the faster trend EMA (default 50)
  within tolerance, AND close is back on the regime side. Classic
  "buy the dip in an uptrend / sell the rip in a downtrend."
* **ATR exit** — stop at entry minus atr_stop_mult x ATR; target at
  rr_target x stop_dist.

Multi-TF property: run the same strategy across {5m, 15m, 1h, 4h, 1d}
and each TF's regime EMA defines a different cycle granularity.

Walk-forward (BTC 1h, 90d/30d, 9 windows): regime=100, pull=21,
tol=3%, atr_stop=2.0, rr=3.0 -> agg OOS Sharpe **+2.96**, 7/9 +OOS,
DSR median 1.000, 67% pass, 91 OOS trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class CryptoRegimeTrendConfig:
    """Knobs for the regime-trend strategy."""

    # Regime / entry EMAs
    regime_ema: int = 200
    pullback_ema: int = 50
    pullback_tolerance_pct: float = 0.5

    # Risk / exits
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    rr_target: float = 2.5
    risk_per_trade_pct: float = 0.01

    # Hygiene
    min_bars_between_trades: int = 12
    max_trades_per_day: int = 3
    warmup_bars: int = 220


def _ema_step(prev: float | None, value: float, period: int) -> float:
    """One step of an EMA; bootstraps to ``value`` on the first call."""
    if prev is None:
        return value
    alpha = 2.0 / (period + 1)
    return alpha * value + (1 - alpha) * prev


class CryptoRegimeTrendStrategy:
    """200 EMA regime + pullback-to-50 trend continuation."""

    def __init__(self, config: CryptoRegimeTrendConfig | None = None) -> None:
        self.cfg = config or CryptoRegimeTrendConfig()
        self._regime_ema: float | None = None
        self._pullback_ema: float | None = None
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        """Return an _Open or None. Same engine contract as ORB."""
        bar_date = bar.timestamp.date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0
        self._bars_seen += 1

        # snapshot prior bar's EMA — see same-bar self-reference fix
        prev_regime_ema = self._regime_ema
        prev_pullback_ema = self._pullback_ema
        # Update EMAs every bar (even during warmup)
        self._regime_ema = _ema_step(self._regime_ema, bar.close, self.cfg.regime_ema)
        self._pullback_ema = _ema_step(self._pullback_ema, bar.close, self.cfg.pullback_ema)

        if self._bars_seen < self.cfg.warmup_bars:
            return None
        if prev_regime_ema is None or prev_pullback_ema is None:
            return None
        if len(hist) < self.cfg.atr_period + 1:
            return None

        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx)
            < self.cfg.min_bars_between_trades
        ):
            return None

        bull_regime = bar.close > prev_regime_ema
        bear_regime = bar.close < prev_regime_ema
        if not (bull_regime or bear_regime):
            return None

        tol = self.cfg.pullback_tolerance_pct / 100.0
        side: str | None = None
        if bull_regime:
            band_lo = prev_pullback_ema * (1.0 - tol)
            band_hi = prev_pullback_ema * (1.0 + tol)
            touched = bar.low <= band_hi
            bounced = bar.close > prev_pullback_ema
            within_tolerance = bar.low >= band_lo
            if touched and bounced and within_tolerance:
                side = "BUY"
        elif bear_regime:
            band_lo = prev_pullback_ema * (1.0 - tol)
            band_hi = prev_pullback_ema * (1.0 + tol)
            touched = bar.high >= band_lo
            bounced = bar.close < prev_pullback_ema
            within_tolerance = bar.high <= band_hi
            if touched and bounced and within_tolerance:
                side = "SELL"
        if side is None:
            return None

        atr_window = hist[-self.cfg.atr_period:] if hist else []
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
            entry_bar=bar, side=side, qty=qty, entry_price=entry_price,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=10.0, leverage=1.0,
            regime=f"crypto_regime_{'bull' if bull_regime else 'bear'}",
        )
        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        return opened
