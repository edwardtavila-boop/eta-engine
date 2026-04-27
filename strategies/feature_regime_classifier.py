"""
EVOLUTIONARY TRADING ALGO  //  strategies.feature_regime_classifier
====================================================================
Multi-feature regime classifier — replaces the failed price-EMA
classifier with one that scores actual signal axes.

Why this exists
---------------
The 2026-04-27 regime-gate experiment (commit 7156a4c) found that
the classical price-EMA + ATR regime classifier doesn't carve
BTC's tape along the same axis as the +6.00 strategy's edge.
deg_avg got worse under gating — the classifier was vetoing the
BEST trades.

User insight (2026-04-27 follow-on): with all the cross-feature
data we have on disk now (5y funding, ETF flow, F&G, on-chain
proxy, sage daily composite), we should classify regime on the
features that ARE correlated with edge — not on price-derived
axes that aren't.

Feature stack
-------------
The classifier scores each enabled feature on a {-1, 0, +1} scale:

  funding_state: per-bar funding rate
    > +funding_extreme   ->  -1  (overheated longs, mean-revert risk)
    < -funding_extreme   ->  +1  (capitulated shorts, bounce expected)
    else                 ->   0

  etf_flow_state: rolling N-day inflow/outflow balance
    > +flow_threshold    ->  +1  (sustained institutional buying)
    < -flow_threshold    ->  -1  (sustained institutional selling)
    else                 ->   0

  fear_greed_state: F&G index normalized
    extreme fear (>=+0.6) ->  +1  (contrarian buy)
    extreme greed (<=-0.6) -> -1  (contrarian sell)
    else                 ->   0

  sage_daily_state: sage composite bias
    direction == 'long'  ->  +1
    direction == 'short' ->  -1
    direction == 'neutral' ->  0

The composite score is a sum of enabled feature states, normalized
to [-1, +1]. That gives us:

  edge_score: float in [-1, +1]

A regime label is then derived from the score:

  "bull_aligned" if score > +bull_threshold
  "bear_aligned" if score < -bull_threshold
  "neutral"      otherwise

This classifier feeds RegimeGatedStrategy via attach_regime_provider
when wrapped in a thin adapter that emits HtfRegimeClassification
objects with sensible (regime, bias, mode) for backwards-compat.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class FeatureRegimeConfig:
    """Knobs for the feature-based regime classifier."""

    # Funding-rate extremes (per 8h, decimal). Above this (or below
    # negative), the funding state contributes to the regime score.
    funding_extreme: float = 0.0005  # +/-0.05% per 8h

    # ETF flow threshold — N-day rolling sum of net flows in M USD.
    # Above this means "sustained inflow"; below negative means
    # "sustained outflow". 0 = filter disabled.
    etf_flow_window_days: int = 5
    etf_flow_threshold: float = 200.0  # M USD over N days

    # Fear & Greed contrarian thresholds. The provider already
    # contrarian-flips so +1 = extreme fear (good for longs),
    # -1 = extreme greed (good for shorts).
    fear_greed_extreme: float = 0.6

    # Sage daily conviction floor — only count sage's direction
    # if conviction >= floor.
    sage_conviction_floor: float = 0.30

    # Composite-score thresholds for bull/bear regime label.
    bull_threshold: float = 0.30
    bear_threshold: float = 0.30

    # Which features to enable. Default: all.
    use_funding: bool = True
    use_etf_flow: bool = True
    use_fear_greed: bool = True
    use_sage_daily: bool = True


@dataclass
class FeatureRegimeOutput:
    """Per-bar classifier output."""

    score: float                            # composite [-1, +1]
    label: str                              # 'bull_aligned'|'bear_aligned'|'neutral'
    bias: str                               # 'long'|'short'|'neutral'
    components: dict[str, float] = field(default_factory=dict)
    n_features_active: int = 0


class FeatureRegimeClassifier:
    """Multi-feature regime classifier — funding + ETF + F&G + sage."""

    def __init__(self, config: FeatureRegimeConfig | None = None) -> None:
        self.cfg = config or FeatureRegimeConfig()
        # Provider plugins; attach what's available.
        self._funding_provider: Callable[[BarData], float] | None = None
        self._etf_flow_provider: Callable[[BarData], float] | None = None
        self._fear_greed_provider: Callable[[BarData], float] | None = None
        self._sage_provider: Callable[[date], object] | None = None
        # Rolling ETF flow window
        self._etf_window: deque[tuple[object, float]] = deque(
            maxlen=self.cfg.etf_flow_window_days + 5,
        )
        # Audit
        self._n_classified: int = 0
        self._regime_counts: dict[str, int] = {
            "bull_aligned": 0, "bear_aligned": 0, "neutral": 0,
        }

    # -- provider attachment -----------------------------------------------

    def attach_funding_provider(
        self, p: Callable[[BarData], float] | None,
    ) -> None:
        self._funding_provider = p

    def attach_etf_flow_provider(
        self, p: Callable[[BarData], float] | None,
    ) -> None:
        self._etf_flow_provider = p

    def attach_fear_greed_provider(
        self, p: Callable[[BarData], float] | None,
    ) -> None:
        self._fear_greed_provider = p

    def attach_sage_daily_provider(
        self, p: Callable[[date], object] | None,
    ) -> None:
        self._sage_provider = p

    # -- audit -------------------------------------------------------------

    @property
    def regime_distribution(self) -> dict[str, int]:
        return dict(self._regime_counts)

    @property
    def n_classified(self) -> int:
        return self._n_classified

    # -- main classify -----------------------------------------------------

    def classify(self, bar: BarData) -> FeatureRegimeOutput:
        components: dict[str, float] = {}
        score_sum = 0.0
        n_active = 0

        # Funding
        if self.cfg.use_funding and self._funding_provider is not None:
            try:
                funding = float(self._funding_provider(bar))
            except (TypeError, ValueError):
                funding = 0.0
            if funding > self.cfg.funding_extreme:
                fs = -1.0
            elif funding < -self.cfg.funding_extreme:
                fs = +1.0
            else:
                fs = 0.0
            components["funding_state"] = fs
            components["funding_raw"] = funding
            if fs != 0.0:
                n_active += 1
            score_sum += fs

        # ETF flow (rolling sum over window)
        if self.cfg.use_etf_flow and self._etf_flow_provider is not None:
            try:
                today_flow = float(self._etf_flow_provider(bar))
            except (TypeError, ValueError):
                today_flow = 0.0
            d = bar.timestamp.date()
            # Append the daily flow only when the date changes
            if not self._etf_window or self._etf_window[-1][0] != d:
                self._etf_window.append((d, today_flow))
            recent = [
                v for _, v in list(self._etf_window)[
                    -self.cfg.etf_flow_window_days:
                ]
            ]
            rolling = sum(recent)
            if rolling > self.cfg.etf_flow_threshold:
                es = +1.0
            elif rolling < -self.cfg.etf_flow_threshold:
                es = -1.0
            else:
                es = 0.0
            components["etf_state"] = es
            components["etf_rolling_M"] = rolling
            if es != 0.0:
                n_active += 1
            score_sum += es

        # Fear & Greed (contrarian-flipped already)
        if self.cfg.use_fear_greed and self._fear_greed_provider is not None:
            try:
                fg = float(self._fear_greed_provider(bar))
            except (TypeError, ValueError):
                fg = 0.0
            if fg >= self.cfg.fear_greed_extreme:
                fgs = +1.0
            elif fg <= -self.cfg.fear_greed_extreme:
                fgs = -1.0
            else:
                fgs = 0.0
            components["fear_greed_state"] = fgs
            components["fear_greed_raw"] = fg
            if fgs != 0.0:
                n_active += 1
            score_sum += fgs

        # Sage daily
        if self.cfg.use_sage_daily and self._sage_provider is not None:
            try:
                verdict = self._sage_provider(bar.timestamp.date())
            except Exception:  # noqa: BLE001 - provider isolation
                verdict = None
            if verdict is not None:
                vdir = getattr(verdict, "direction", "neutral")
                vconv = float(getattr(verdict, "conviction", 0.0))
                if vconv >= self.cfg.sage_conviction_floor:
                    if vdir == "long":
                        ss = +1.0
                    elif vdir == "short":
                        ss = -1.0
                    else:
                        ss = 0.0
                else:
                    ss = 0.0
                components["sage_state"] = ss
                components["sage_conviction"] = vconv
                if ss != 0.0:
                    n_active += 1
                score_sum += ss

        # Normalize to [-1, +1] by dividing by max possible (number of enabled features)
        n_enabled = sum([
            self.cfg.use_funding,
            self.cfg.use_etf_flow,
            self.cfg.use_fear_greed,
            self.cfg.use_sage_daily,
        ])
        score = score_sum / max(n_enabled, 1)

        if score > self.cfg.bull_threshold:
            label = "bull_aligned"
            bias = "long"
        elif score < -self.cfg.bear_threshold:
            label = "bear_aligned"
            bias = "short"
        else:
            label = "neutral"
            bias = "neutral"

        self._n_classified += 1
        self._regime_counts[label] = self._regime_counts.get(label, 0) + 1

        return FeatureRegimeOutput(
            score=score,
            label=label,
            bias=bias,
            components=components,
            n_features_active=n_active,
        )


def make_feature_regime_provider(
    classifier: FeatureRegimeClassifier,
    daily_bars: list,  # noqa: ANN001 - flexible type
) -> Callable[[date], object]:
    """Adapt a FeatureRegimeClassifier into the regime-provider
    callable shape expected by ``RegimeGatedStrategy.attach_regime_provider``.

    Pre-classifies every daily bar at startup, then exposes a
    ``provider(date) -> HtfRegimeClassification``-shaped object.
    The returned object has ``regime`` / ``bias`` / ``mode`` attrs
    matching the existing HtfRegimeClassification contract.
    """
    from eta_engine.strategies.htf_regime_classifier import (
        HtfRegimeClassification,
    )

    classifications: dict = {}
    for b in daily_bars:
        out = classifier.classify(b)
        # Map our richer output to the existing classification shape:
        # bull_aligned/bear_aligned -> trending; neutral -> ranging
        regime = "trending" if out.label != "neutral" else "ranging"
        mode = "trend_follow" if out.label != "neutral" else "mean_revert"
        classifications[b.timestamp.date()] = HtfRegimeClassification(
            bias=out.bias, regime=regime, mode=mode,
            close=b.close,
            components={
                "edge_score": out.score,
                "n_features_active": float(out.n_features_active),
                **{k: v for k, v in out.components.items()},
            },
        )

    sorted_dates = sorted(classifications.keys())

    def _provider(d: object) -> HtfRegimeClassification:
        for prev in reversed(sorted_dates):
            if prev <= d:
                return classifications[prev]
        return HtfRegimeClassification(
            bias="neutral", regime="volatile", mode="skip",
        )

    return _provider
