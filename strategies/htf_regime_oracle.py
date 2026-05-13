"""
EVOLUTIONARY TRADING ALGO  //  strategies.htf_regime_oracle
=============================================================
Higher-timeframe regime + conviction oracle.

User insight (2026-04-27): "wouldn't these [Tier-4 signals] make
more sense when looking at higher time frames and helping to
determine regime and trading direction confluence for bigger
confluence trades with a little higher risk?"

Yes. The previous abstraction layered Tier-4 signals as 1h entry
filters — wrong granularity. The signals are inherently daily/
weekly:

  * ETF flows update daily after market close
  * Fear & Greed updates daily
  * LTH-supply phases shift over weeks/months
  * Macro tailwind (DXY/SPY) is a daily-bar story
  * HTF regime EMA is a structural-cycle gate

The right pattern: read all of these on their natural cadence,
fuse into a single (direction, conviction) tuple, and let the
intraday execution layer ASK the oracle "which way + how much
conviction" instead of asking "should I block this entry."

Then the strategy can size positions by conviction — bigger
trades when the ensemble lines up, smaller (or skip) when it
doesn't. This is how discretionary traders actually use these
signals.

Composite score formula
-----------------------
Each component returns a value in [-1, +1] (positive = bullish).
Default weights (sum to 1.0):

  ETF flow direction              0.30  (validated +1.32 OOS lift)
  HTF regime EMA (price > slow)   0.25  (structural)
  LTH-supply proxy                0.15  (multi-month phase)
  Macro tailwind (DXY + SPY)      0.15  (risk-on/off)
  Fear & Greed (contrarian)       0.15  (sentiment)

Composite = sum(weight_i * score_i).
Direction = "long" if composite > +threshold,
            "short" if composite < -threshold,
            "neutral" otherwise.
Conviction = |composite|, clipped to [0, 1].

Operators tune the weights + threshold via HtfRegimeOracleConfig.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class HtfRegimeOracleConfig:
    """Knobs for the HTF regime fusion."""

    # Component weights (must be non-negative; normalized to sum=1)
    weight_etf_flow: float = 0.30
    weight_htf_ema: float = 0.25
    weight_lth_proxy: float = 0.15
    weight_macro: float = 0.15
    weight_fear_greed: float = 0.15

    # HTF EMA period — used by the in-strategy EMA, not a provider.
    # On 1h bars, 800 ≈ 33 days = a structural cycle gate.
    htf_ema_period: int = 800

    # Direction threshold — composite must exceed |this| to register
    # a non-neutral regime. 0.0 = directional whenever any net bias
    # exists; 0.2 = require modest agreement.
    direction_threshold: float = 0.20

    # Smoothing — exponentially average the daily score so single-day
    # spikes don't whipsaw the regime. period in days. 0 = disabled.
    smoothing_period_days: int = 5

    # ETF flow normalization: divide raw flow (USD M) by this scale
    # before clipping to [-1, +1]. Default 500 = a 500M day saturates.
    etf_flow_scale_usd_m: float = 500.0


@dataclass
class HtfRegimeReport:
    """Oracle output. Mutable for ease of attaching audit metadata."""

    direction: str  # "long" / "short" / "neutral"
    conviction: float  # 0.0 - 1.0
    composite: float  # -1.0 to +1.0 (signed)
    components: dict[str, float] = field(default_factory=dict)
    timestamp: datetime | None = None


class HtfRegimeOracle:
    """Fuses Tier-4 daily signals into a (direction, conviction) tuple.

    Construction takes optional providers — same callables the macro
    confluence strategy uses. Missing providers contribute 0 to the
    composite (don't move the score either way).

    The oracle keeps a rolling smoothed score so the regime doesn't
    flip on a single-day outlier (e.g. a one-day ETF outflow that
    reverses the next day).
    """

    def __init__(
        self,
        config: HtfRegimeOracleConfig | None = None,
        *,
        etf_flow_provider: Callable[[BarData], float] | None = None,
        lth_provider: Callable[[BarData], float] | None = None,
        fear_greed_provider: Callable[[BarData], float] | None = None,
        macro_provider: Callable[[BarData], float] | None = None,
    ) -> None:
        self.cfg = config or HtfRegimeOracleConfig()
        self._etf = etf_flow_provider
        self._lth = lth_provider
        self._fg = fear_greed_provider
        self._macro = macro_provider
        # In-strategy HTF EMA — updated on each bar
        self._htf_ema: float | None = None
        # Smoothed composite score (exponential)
        self._smoothed_composite: float | None = None

    # -- per-bar maintenance -------------------------------------------------

    def update_htf_ema(self, close: float) -> None:
        """Strategy must call this once per bar BEFORE asking for regime.

        Keeps the HTF EMA fresh on the same bar stream the strategy is
        consuming — no second timeframe required.
        """
        period = self.cfg.htf_ema_period
        if period <= 0:
            self._htf_ema = None
            return
        if self._htf_ema is None:
            self._htf_ema = close
            return
        alpha = 2.0 / (period + 1)
        self._htf_ema = alpha * close + (1 - alpha) * self._htf_ema

    # -- regime fusion -------------------------------------------------------

    def regime_for(self, bar: BarData) -> HtfRegimeReport:
        """Compute the regime for the given bar.

        Returns a HtfRegimeReport with direction, conviction, signed
        composite, and a per-component dict for audit. Conviction is
        always in [0, 1]; composite is in [-1, +1] (signed).
        """
        components: dict[str, float] = {}

        # Component 1: ETF flow direction (positive = inflow)
        etf_score = 0.0
        if self._etf is not None:
            try:
                raw = float(self._etf(bar))
            except Exception:  # noqa: BLE001
                raw = 0.0
            etf_score = max(-1.0, min(1.0, raw / max(self.cfg.etf_flow_scale_usd_m, 1.0)))
        components["etf_flow"] = etf_score

        # Component 2: HTF EMA (price > slow EMA = bullish)
        htf_score = 0.0
        if self._htf_ema is not None and self._htf_ema > 0:
            # Distance in % of EMA, clipped to [-1, +1]
            ratio = (bar.close - self._htf_ema) / self._htf_ema
            htf_score = max(-1.0, min(1.0, ratio * 10.0))  # 10% deviation saturates
        components["htf_ema"] = htf_score

        # Component 3: LTH proxy (already in [-1, +1])
        lth_score = 0.0
        if self._lth is not None:
            try:
                lth_score = max(-1.0, min(1.0, float(self._lth(bar))))
            except Exception:  # noqa: BLE001
                lth_score = 0.0
        components["lth_proxy"] = lth_score

        # Component 4: Fear & Greed (contrarian — already in [-1, +1])
        fg_score = 0.0
        if self._fg is not None:
            try:
                fg_score = max(-1.0, min(1.0, float(self._fg(bar))))
            except Exception:  # noqa: BLE001
                fg_score = 0.0
        components["fear_greed"] = fg_score

        # Component 5: Macro tailwind (already in [-1, +1])
        macro_score = 0.0
        if self._macro is not None:
            try:
                macro_score = max(-1.0, min(1.0, float(self._macro(bar))))
            except Exception:  # noqa: BLE001
                macro_score = 0.0
        components["macro"] = macro_score

        # Weighted composite
        weights = {
            "etf_flow": self.cfg.weight_etf_flow,
            "htf_ema": self.cfg.weight_htf_ema,
            "lth_proxy": self.cfg.weight_lth_proxy,
            "fear_greed": self.cfg.weight_fear_greed,
            "macro": self.cfg.weight_macro,
        }
        weight_sum = sum(weights.values())
        composite = 0.0 if weight_sum <= 0 else sum(components[k] * (weights[k] / weight_sum) for k in components)
        composite = max(-1.0, min(1.0, composite))

        # Smoothing
        if self.cfg.smoothing_period_days > 0:
            if self._smoothed_composite is None:
                self._smoothed_composite = composite
            else:
                alpha = 2.0 / (self.cfg.smoothing_period_days + 1)
                self._smoothed_composite = alpha * composite + (1 - alpha) * self._smoothed_composite
            effective = self._smoothed_composite
        else:
            effective = composite

        # Direction + conviction
        threshold = self.cfg.direction_threshold
        if effective > threshold:
            direction = "long"
        elif effective < -threshold:
            direction = "short"
        else:
            direction = "neutral"
        conviction = min(1.0, abs(effective))

        return HtfRegimeReport(
            direction=direction,
            conviction=conviction,
            composite=effective,
            components=components,
            timestamp=bar.timestamp,
        )
