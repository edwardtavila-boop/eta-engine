"""FX Range Strategy — mean-reversion for 6E (Euro) and ZN (10Y Treasury).

These assets trade in ranges, not trends. Mean-reversion edges:
  - Bollinger Band touches with volume confirmation
  - RSI oversold/overbought at range extremes
  - Support/resistance at prior session highs/lows
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class RangeConfig:
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 14
    rsi_oversold: int = 30
    rsi_overbought: int = 70
    volume_z_lookback: int = 20
    min_volume_z: float = 0.3
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    rr_target: float = 2.0
    risk_per_trade_pct: float = 0.005
    min_bars_between_trades: int = 6
    max_trades_per_day: int = 4
    warmup_bars: int = 50


class RangeStrategy:
    """FX/Rates mean-reversion — fade extremes in range-bound markets."""

    def __init__(self, cfg: RangeConfig | None = None) -> None:
        self.cfg = cfg or RangeConfig()
        self._close_window: deque[float] = deque(maxlen=self.cfg.bb_period * 2)
        self._volume_window: deque[float] = deque(maxlen=self.cfg.volume_z_lookback)
        self._tr_window: deque[float] = deque(maxlen=self.cfg.atr_period)
        self._rsi: float | None = None
        self._gains: deque[float] = deque(maxlen=self.cfg.rsi_period)
        self._losses: deque[float] = deque(maxlen=self.cfg.rsi_period)
        self._bars_since_last_trade: int = 999
        self._trades_today: int = 0
        self._bars_seen: int = 0

    def maybe_enter(self, bar: BarData, hist: list[BarData], equity: float, config: BacktestConfig) -> _Open | None:
        self._bars_seen += 1
        if self._bars_seen < self.cfg.warmup_bars:
            self._update(bar, hist); return None
        self._update(bar, hist)
        self._bars_since_last_trade += 1
        if self._bars_since_last_trade < self.cfg.min_bars_between_trades: return None
        if self._trades_today >= self.cfg.max_trades_per_day: return None

        side = self._detect_reversion(bar)
        if side is None: return None

        atr = self._atr()
        stop_dist = atr * self.cfg.atr_stop_mult
        risk_usd = equity * self.cfg.risk_per_trade_pct
        if side == "BUY":
            entry = bar.close; stop = entry - stop_dist; target = entry + stop_dist * self.cfg.rr_target
        else:
            entry = bar.close; stop = entry + stop_dist; target = entry - stop_dist * self.cfg.rr_target
        qty = risk_usd / max(stop_dist, 1e-9)
        if qty <= 0: return None
        self._bars_since_last_trade = 0; self._trades_today += 1
        from eta_engine.backtest.engine import _Open
        return _Open(entry_bar=bar, side=side, qty=qty, entry_price=entry, stop=stop, target=target, risk_usd=risk_usd, confluence=6.0, leverage=1.0, regime=f"range_{side.lower()}")

    def _update(self, bar: BarData, hist: list[BarData]) -> None:
        self._close_window.append(bar.close); self._volume_window.append(bar.volume)
        prev = hist[-1].close if hist else bar.open
        chg = bar.close - prev
        self._gains.append(max(chg, 0)); self._losses.append(max(-chg, 0))
        tr = max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev))
        self._tr_window.append(tr)

    def _atr(self) -> float:
        if len(self._tr_window) < self.cfg.atr_period: return 1.0
        return sum(self._tr_window) / len(self._tr_window)

    def _bb(self) -> tuple[float, float, float]:
        if len(self._close_window) < self.cfg.bb_period: return (0, 0, 0)
        prices = list(self._close_window)[-self.cfg.bb_period:]
        ma = sum(prices) / len(prices)
        std = (sum((p - ma) ** 2 for p in prices) / len(prices)) ** 0.5
        return (ma - self.cfg.bb_std * std, ma, ma + self.cfg.bb_std * std)

    def _rsi_val(self) -> float:
        if len(self._gains) < self.cfg.rsi_period: return 50.0
        avg_gain = sum(self._gains) / self.cfg.rsi_period
        avg_loss = sum(self._losses) / self.cfg.rsi_period
        if avg_loss < 1e-9: return 100.0
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    def _detect_reversion(self, bar: BarData) -> str | None:
        volumes = list(self._volume_window)
        if len(volumes) < self.cfg.volume_z_lookback: return None
        mean_v = sum(volumes) / len(volumes)
        std_v = (sum((v - mean_v) ** 2 for v in volumes) / len(volumes)) ** 0.5
        if std_v > 0 and (bar.volume - mean_v) / std_v < self.cfg.min_volume_z: return None
        lower, mid, upper = self._bb()
        if lower == 0: return None
        rsi = self._rsi_val()
        close = bar.close
        if close <= lower and rsi < self.cfg.rsi_oversold: return "BUY"
        if close >= upper and rsi > self.cfg.rsi_overbought: return "SELL"
        return None


def eur_range_preset() -> RangeConfig:
    """Euro FX (6E) — tight ranges, 0.005-0.01 ATR, BB(20,2) + RSI."""
    return RangeConfig(bb_period=20, bb_std=2.0, rsi_period=14, rsi_oversold=30, rsi_overbought=70, volume_z_lookback=20, min_volume_z=0.3, atr_period=14, atr_stop_mult=1.5, rr_target=2.0, risk_per_trade_pct=0.005, min_bars_between_trades=6, max_trades_per_day=4, warmup_bars=50)


def zn_range_preset() -> RangeConfig:
    """10Y Treasury (ZN) — rates range, wider BB(20,2.5), tighter stop."""
    return RangeConfig(bb_period=20, bb_std=2.5, rsi_period=14, rsi_oversold=25, rsi_overbought=75, volume_z_lookback=20, min_volume_z=0.3, atr_period=14, atr_stop_mult=1.0, rr_target=2.5, risk_per_trade_pct=0.005, min_bars_between_trades=8, max_trades_per_day=3, warmup_bars=50)
