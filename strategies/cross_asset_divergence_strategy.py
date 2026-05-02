"""
EVOLUTIONARY TRADING ALGO  //  strategies.cross_asset_divergence_strategy
==========================================================================
Cross-asset pairs / spread mean-reversion strategy.

Markets move together until they don't — and when they diverge, the
spread tends to mean-revert. This strategy trades the z-score of
asset ratios (NQ/ES, MNQ/MES, BTC/ETH) using the same mechanical
trigger framework as sweep_reclaim.

Why this edge exists
---------------------
Correlated assets share common risk factors. When one over- or
under-performs relative to its correlated pair by more than 2σ, the
divergence typically reverts within hours. This is the same mechanic
that pairs-trading desks exploit, translated into bar-by-bar
mechanical triggers.

Mechanic
--------
1. Compute the ratio between the primary instrument's close and the
   reference instrument's close for each bar in the history window.
   - MNQ/NQ: ratio = NQ_close / ES_close (tracked as z-score)
   - BTC/ETH: ratio = BTC_close / ETH_close
2. Z-score of the ratio over lookback window.
3. When z-score > entry_threshold (primary overperforming), SHORT
   the primary (bet on mean-reversion of ratio).
4. When z-score < -entry_threshold (primary underperforming), LONG
   the primary.
5. The reference instrument's data is consumed as a separate bar
   stream — caller must provide ES or ETH bars aligned to the same
   timestamps.

Designed to be wrapped by ConfluenceScorecardStrategy. Provides
genuine diversification because the primary signal source (asset
ratio) is orthogonal to single-instrument price action signals.

Configurable for asset class
-----------------------------
* MNQ vs ES: z_lookback=100, entry_z=2.0, atr_stop_mult=1.0, rr=2.0
* NQ vs ES: z_lookback=100, entry_z=2.0, atr_stop_mult=1.0, rr=2.0
* BTC vs ETH: z_lookback=168, entry_z=2.0, atr_stop_mult=1.5, rr=2.5
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class CrossAssetDivergenceConfig:
    z_lookback: int = 100
    entry_z_threshold: float = 2.0
    min_z_threshold: float = 1.5

    volume_z_lookback: int = 20
    min_volume_z: float = 0.3

    atr_period: int = 14
    atr_stop_mult: float = 1.0
    rr_target: float = 2.0
    risk_per_trade_pct: float = 0.005

    min_bars_between_trades: int = 12
    max_trades_per_day: int = 2
    warmup_bars: int = 100

    allow_long: bool = True
    allow_short: bool = True


class CrossAssetDivergenceStrategy:

    def __init__(self, config: CrossAssetDivergenceConfig | None = None) -> None:
        self.cfg = config or CrossAssetDivergenceConfig()
        self._ratios: deque[float] = deque(maxlen=self.cfg.z_lookback + 5)
        self._reference_provider: Callable[[BarData], float] | None = None
        self._volume_window: deque[float] = deque(maxlen=self.cfg.volume_z_lookback)
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None
        self._n_long_sig: int = 0
        self._n_short_sig: int = 0
        self._n_vol_reject: int = 0
        self._n_fired: int = 0
        self._n_no_ref: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "long_signals": self._n_long_sig,
            "short_signals": self._n_short_sig,
            "vol_rejects": self._n_vol_reject,
            "entries_fired": self._n_fired,
            "no_reference_data": self._n_no_ref,
        }

    def attach_reference_provider(
        self, provider: Callable[[BarData], float] | None,
    ) -> None:
        self._reference_provider = provider

    def _compute_z_score(self) -> float:
        if len(self._ratios) < self.cfg.z_lookback // 2:
            return 0.0
        recent = list(self._ratios)[-self.cfg.z_lookback:]
        if not recent:
            return 0.0
        mean = sum(recent) / len(recent)
        var = sum((r - mean) ** 2 for r in recent) / len(recent)
        std = var ** 0.5
        if std <= 0.0:
            return 0.0
        return (recent[-1] - mean) / std

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

        if self._reference_provider is not None:
            try:
                ref_price = float(self._reference_provider(bar))
                if ref_price > 0:
                    self._ratios.append(bar.close / ref_price)
            except (TypeError, ValueError):
                pass

        if self._bars_seen < self.cfg.warmup_bars:
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx) < self.cfg.min_bars_between_trades
        ):
            return None

        z_score = self._compute_z_score()
        if abs(z_score) < self.cfg.min_z_threshold:
            return None

        side: str | None = None
        if self.cfg.allow_short and z_score > self.cfg.entry_z_threshold:
            side = "SELL"
            self._n_short_sig += 1
        elif self.cfg.allow_long and z_score < -self.cfg.entry_z_threshold:
            side = "BUY"
            self._n_long_sig += 1

        if side is None:
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
            regime=f"xasset_div_{side.lower()}_z{z_score:.1f}",
        )


def mnq_vs_es_divergence_preset() -> CrossAssetDivergenceConfig:
    return CrossAssetDivergenceConfig(
        z_lookback=100, entry_z_threshold=2.0, min_z_threshold=1.5,
        volume_z_lookback=20, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=1.0, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=100,
    )


def nq_vs_es_divergence_preset() -> CrossAssetDivergenceConfig:
    return CrossAssetDivergenceConfig(
        z_lookback=100, entry_z_threshold=2.0, min_z_threshold=1.5,
        volume_z_lookback=20, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=1.0, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=100,
    )


def btc_vs_eth_divergence_preset() -> CrossAssetDivergenceConfig:
    return CrossAssetDivergenceConfig(
        z_lookback=168, entry_z_threshold=2.0, min_z_threshold=1.5,
        volume_z_lookback=24, min_volume_z=0.2,
        atr_period=14, atr_stop_mult=1.5, rr_target=2.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=168,
    )
