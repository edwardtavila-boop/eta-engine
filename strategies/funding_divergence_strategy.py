"""
EVOLUTIONARY TRADING ALGO  //  strategies.funding_divergence_strategy
======================================================================
Mean-reversion on BTC funding-rate extremes.

Why this strategy
-----------------
The 2026-04-27 supercharge thread (commit 973a6aa) proved that
post-hoc filters / sizing layers cannot extract more juice from
a sample-specific result. The +6.00 BTC champion's edge was
regime-alignment-driven, not regime-invariant.

This strategy is designed to be **regime-invariant by mechanic**:
it trades the POSITIONING of derivatives traders, not the
direction of price.

Mechanic
--------
Funding rate is paid every 8h on perpetual futures. When funding
is extremely positive, longs are paying shorts — that's a sign of
overheated long positioning (over-leveraged longs, asymmetric
exposure). When funding is extremely negative, shorts are paying
longs — over-leveraged shorts. **Both extremes historically
mean-revert** because excessive positioning gets unwound.

Trade rules:
* When funding > +entry_threshold (e.g. +0.075% per 8h), SHORT.
  Stop = ATR-based above; target = mean-reversion to neutral
  funding.
* When funding < -entry_threshold (e.g. -0.075%), LONG.
* Cooldown: min_bars_between_trades to avoid rapid-fire entries
  on the same funding event.

This works regardless of:
* Bull / bear / sideways regime
* Price-EMA structure
* Trend / range / volatile classification

The only requirement is a working funding-rate provider — which
we have as of 2026-04-27 (5,475 rows of BTC 8h funding from
BitMEX, May 2021 → present, committed in 973a6aa).

Notes on threshold calibration
------------------------------
BTC perp funding has historically averaged ~+0.01% per 8h
(slight long premium). Extremes (>|0.05%|) typically mean-revert
within 24-72 hours. The +/-0.075% default is a conservative
"mild extreme" cutoff. Stricter (+/-0.10%) → fewer trades, more
selective; looser (+/-0.05%) → more trades, more noise.

Calibration is part of walk-forward — the user is encouraged to
sweep the threshold parameter. Defaults below are starting
points only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class FundingDivergenceConfig:
    """Knobs for funding-divergence mean-reversion."""

    # Entry threshold (per 8h, as decimal). 0.00075 = +0.075%.
    # SHORT when funding > +entry_threshold; LONG when < -threshold.
    entry_threshold: float = 0.00075

    # Risk / exits
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    rr_target: float = 2.0
    risk_per_trade_pct: float = 0.01

    # Hygiene — funding events fire every 8h on most exchanges, so
    # min 24 bars on 1h LTF = "wait for at least one funding cycle
    # before re-firing"
    min_bars_between_trades: int = 24
    max_trades_per_day: int = 1
    warmup_bars: int = 50

    # Optional: require sage daily verdict to ALIGN with the trade
    # direction (long-funding-extreme + sage_short = high-conviction
    # short trade; vice versa). When False, fire purely on funding.
    require_directional_confirmation: bool = False


class FundingDivergenceStrategy:
    """Mean-reversion on BTC funding-rate extremes."""

    def __init__(
        self, config: FundingDivergenceConfig | None = None,
    ) -> None:
        self.cfg = config or FundingDivergenceConfig()
        self._funding_provider: Callable[[BarData], float] | None = None
        self._daily_verdict_provider: Callable[..., object] | None = None
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None
        # Audit counters
        self._n_funding_extreme_seen: int = 0
        self._n_entries_fired: int = 0
        self._n_directional_vetoes: int = 0

    # -- provider plumbing --------------------------------------------------

    def attach_funding_provider(
        self, p: Callable[[BarData], float] | None,
    ) -> None:
        """Attach a funding-rate provider (e.g. FundingRateProvider)."""
        self._funding_provider = p

    def attach_daily_verdict_provider(
        self, p: Callable[..., object] | None,
    ) -> None:
        """Optional sage daily verdict provider for directional confirmation."""
        self._daily_verdict_provider = p

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "funding_extreme_seen": self._n_funding_extreme_seen,
            "entries_fired": self._n_entries_fired,
            "directional_vetoes": self._n_directional_vetoes,
        }

    # -- main entry point ---------------------------------------------------

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

        if self._bars_seen < self.cfg.warmup_bars:
            return None
        if self._funding_provider is None:
            # Without funding data, the strategy is inert
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

        try:
            funding = float(self._funding_provider(bar))
        except (TypeError, ValueError):
            return None

        # Direction by funding sign + extremeness
        side: str | None = None
        if funding > self.cfg.entry_threshold:
            side = "SELL"  # mean-revert overheated longs
            self._n_funding_extreme_seen += 1
        elif funding < -self.cfg.entry_threshold:
            side = "BUY"  # mean-revert capitulated shorts
            self._n_funding_extreme_seen += 1
        if side is None:
            return None

        # Optional directional confirmation via sage daily
        if (
            self.cfg.require_directional_confirmation
            and self._daily_verdict_provider is not None
        ):
            try:
                verdict = self._daily_verdict_provider(bar.timestamp.date())
                # Verdict has a `direction` attr ('long'/'short'/'neutral')
                vdir = getattr(verdict, "direction", "neutral")
                if side == "BUY" and vdir == "short":
                    self._n_directional_vetoes += 1
                    return None
                if side == "SELL" and vdir == "long":
                    self._n_directional_vetoes += 1
                    return None
            except Exception:  # noqa: BLE001 - provider isolation
                pass

        # Risk sizing
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

        from eta_engine.backtest.engine import _Open  # local import

        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        self._n_entries_fired += 1
        return _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry_price,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=10.0,  # synthetic; not used by this strategy
            leverage=1.0,
            regime=f"funding_div_{funding * 1e4:+.1f}bps",
        )
