"""
EVOLUTIONARY TRADING ALGO  //  strategies.funding_rate_strategy
================================================================
Funding-rate-based directional strategy.

IMPORTANT: This is the companion to funding_divergence_strategy.py.
Where funding_divergence fades EXTREME funding (contrarian
mean-reversion), this strategy follows PERSISTENT funding (momentum).

Why two funding strategies
--------------------------
Funding rate carries information about BOTH:
  A. Extreme positioning → means reversion is likely
     (funding_divergence_strategy — fades extremes)
  B. Persistent directional flow → trend continuation
     (THIS strategy — rides the funding trend)

When funding has been positive for 3+ consecutive 8h cycles (>24h),
it signals persistent bullish positioning — the market is trending up
and longs are paying shorts to stay in the trade. Enter LONG to
collect funding + ride the momentum.

When funding has been negative for 3+ consecutive cycles, enter SHORT.

Mechanic
--------
1. Track funding rate every bar (from BTCFUND_8h CSV or bybit_ws).
2. Maintain a rolling window of funding rate signs: +1 if funding > 0,
   -1 if funding < 0. Persistence = sum(signs) / count of signs.
3. Enter in the direction of persistent funding when:
   - Persistence score exceeds threshold (e.g., > 0.6 → 4+/6 cycles same sign)
   - Close is above/below EMA trend filter (structural confirmation)
   - Volume z-score confirms participation
4. Stop: ATR-based. Target: RR scaled to asset.

The key difference from funding_divergence: thresholds are MUCH lower
(0.01% vs 0.075%) and the direction follows funding, not fades it.

Designed to be wrapped by ConfluenceScorecardStrategy.

Data requirement: funding rate provider (same provider used by
funding_divergence_strategy — BTCFUND_8h CSV reader).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class FundingRateStrategyConfig:
    persistence_lookback: int = 6
    persistence_threshold: float = 0.60

    ema_period: int = 21
    require_pullback: bool = True

    volume_z_lookback: int = 20
    min_volume_z: float = 0.2

    atr_period: int = 14
    atr_stop_mult: float = 1.5
    rr_target: float = 2.0
    risk_per_trade_pct: float = 0.005

    min_bars_between_trades: int = 12
    max_trades_per_day: int = 2
    warmup_bars: int = 50

    allow_long: bool = True
    allow_short: bool = True


class FundingRateStrategy:

    def __init__(self, config: FundingRateStrategyConfig | None = None) -> None:
        self.cfg = config or FundingRateStrategyConfig()
        self._funding_provider: Callable[[BarData], float] | None = None
        self._funding_signs: deque[int] = deque(maxlen=self.cfg.persistence_lookback + 5)
        # Track last funding VALUE so we only push to _funding_signs on a
        # cycle change (every ~8h on Binance perps), not on every bar.
        self._last_funding_value: float | None = None
        self._ema: float | None = None
        self._volume_window: deque[float] = deque(maxlen=self.cfg.volume_z_lookback)
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None
        self._n_long_sig: int = 0
        self._n_short_sig: int = 0
        self._n_vol_reject: int = 0
        self._n_trend_veto: int = 0
        self._n_fired: int = 0
        self._n_provider_nan: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "long_signals": self._n_long_sig,
            "short_signals": self._n_short_sig,
            "vol_rejects": self._n_vol_reject,
            "trend_vetoes": self._n_trend_veto,
            "entries_fired": self._n_fired,
            "provider_nan": self._n_provider_nan,
        }

    def attach_funding_provider(
        self, p: Callable[[BarData], float] | None,
    ) -> None:
        self._funding_provider = p

    def _funding_persistence(self) -> float:
        if len(self._funding_signs) < self.cfg.persistence_lookback:
            return 0.0
        recent = list(self._funding_signs)[-self.cfg.persistence_lookback:]
        pos_count = sum(1 for s in recent if s > 0)
        neg_count = sum(1 for s in recent if s < 0)
        total = pos_count + neg_count
        if total == 0:
            return 0.0
        return max(pos_count, neg_count) / total

    def _funding_direction(self) -> str:
        if len(self._funding_signs) < self.cfg.persistence_lookback:
            return "neutral"
        recent = list(self._funding_signs)[-self.cfg.persistence_lookback:]
        pos_count = sum(1 for s in recent if s > 0)
        neg_count = sum(1 for s in recent if s < 0)
        if pos_count > neg_count:
            return "positive"
        if neg_count > pos_count:
            return "negative"
        return "neutral"

    def _update_ema(self, close: float) -> None:
        if self._ema is None:
            self._ema = close
        else:
            alpha = 2.0 / (self.cfg.ema_period + 1)
            self._ema = alpha * close + (1 - alpha) * self._ema

    def _volume_z_score(self, bar: BarData) -> float:
        if len(self._volume_window) < self.cfg.volume_z_lookback:
            return 0.0
        vols = list(self._volume_window)
        mean = sum(vols) / len(vols)
        var = sum((v - mean) ** 2 for v in vols) / len(vols)
        std = var ** 0.5
        if std <= 0.0:
            return 0.0
        return (bar.volume - mean) / std

    def maybe_enter(
        self, bar: BarData, hist: list[BarData],
        equity: float, config: BacktestConfig,
    ) -> _Open | None:
        bar_date = bar.timestamp.date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0

        self._bars_seen += 1
        self._volume_window.append(bar.volume)
        self._update_ema(bar.close)

        if self._funding_provider is not None:
            try:
                funding = float(self._funding_provider(bar))
            except (TypeError, ValueError):
                funding = math.nan
            # Provider returns NaN when CSV reading is stale — skip the
            # update so we don't poison _last_funding_value (NaN
            # comparisons silently return False forever after).
            if math.isnan(funding):
                self._n_provider_nan += 1
            else:
                # FIX: only push to _funding_signs when the funding VALUE
                # changes (every ~8h cycle), not on every bar.  The legacy
                # code appended on every 5m / 1h bar — so "6 of last 6
                # cycles persistent" was actually "6 of last 6 bars sampled
                # the same stale 8h funding value", which was trivially
                # always satisfied and rendered persistence_threshold a
                # no-op.  This fix makes persistence_lookback=6 mean what
                # it claims (6 funding cycles, ~48h on Binance perps).
                if (
                    self._last_funding_value is None
                    or abs(funding - self._last_funding_value) > 1e-12
                ):
                    self._funding_signs.append(
                        1 if funding > 0 else (-1 if funding < 0 else 0),
                    )
                    self._last_funding_value = funding

        if self._bars_seen < self.cfg.warmup_bars:
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx) < self.cfg.min_bars_between_trades
        ):
            return None

        persistence = self._funding_persistence()
        if persistence < self.cfg.persistence_threshold:
            return None

        direction = self._funding_direction()
        side: str | None = None
        if self.cfg.allow_long and direction == "positive":
            side = "BUY"
            self._n_long_sig += 1
        elif self.cfg.allow_short and direction == "negative":
            side = "SELL"
            self._n_short_sig += 1

        if side is None:
            return None

        if self.cfg.require_pullback and self._ema is not None:
            # FIX: filter was INVERTED. The pullback gate's purpose is to
            # require price has reclaimed the trend EMA before entering
            # in the funding-implied direction. Old code blocked BUYs
            # when price was ABOVE the EMA (the exact entries we WANT)
            # and let through BUYs below the EMA (against trend).
            if side == "BUY" and bar.close < self._ema:
                self._n_trend_veto += 1
                return None
            if side == "SELL" and bar.close > self._ema:
                self._n_trend_veto += 1
                return None

        vz = self._volume_z_score(bar)
        if vz < self.cfg.min_volume_z:
            self._n_vol_reject += 1
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

        entry = bar.close
        if side == "BUY":
            stop = entry - stop_dist
            stop_dist_actual = entry - stop
            target = entry + self.cfg.rr_target * stop_dist_actual
        else:
            stop = entry + stop_dist
            stop_dist_actual = stop - entry
            target = entry - self.cfg.rr_target * stop_dist_actual

        from eta_engine.backtest.engine import _Open

        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        self._n_fired += 1
        return _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=7.0, leverage=1.0,
            regime=f"fund_rate_{side.lower()}_p{persistence:.1f}",
        )


def btc_funding_rate_preset() -> FundingRateStrategyConfig:
    return FundingRateStrategyConfig(
        persistence_lookback=6, persistence_threshold=0.60,
        ema_period=21, require_pullback=True,
        volume_z_lookback=24, min_volume_z=0.2,
        atr_period=14, atr_stop_mult=1.5, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
    )


def eth_funding_rate_preset() -> FundingRateStrategyConfig:
    return FundingRateStrategyConfig(
        persistence_lookback=6, persistence_threshold=0.50,
        ema_period=21, require_pullback=True,
        volume_z_lookback=24, min_volume_z=0.2,
        atr_period=14, atr_stop_mult=1.8, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
    )
