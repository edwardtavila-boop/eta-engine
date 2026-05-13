"""
EVOLUTIONARY TRADING ALGO  //  strategies.htf_regime_classifier
================================================================
HTF regime classifier: turns a bar series into (bias, regime, mode).

User insight (2026-04-27 follow-up): "higher time frame to determine
current time frame location... last month has been bearish, our
strategy should mainly be bearish with mean reversion fit for HTF and
directional being mainly HTF... after confirming regime we can scalp
lower time frames."

This module is the regime layer the user described. It takes ONE
input — a bar series — and produces a structured classification:

  bias:    "long" | "short" | "neutral"
  regime:  "trending" | "ranging" | "volatile"
  mode:    "trend_follow" | "mean_revert" | "skip"

The downstream strategy (HtfRoutedStrategy) reads the classification
and dispatches to the appropriate execution logic. This separation
makes the regime read auditable + testable independently of any
particular strategy.

Classification rules
--------------------
1. **Bias** is set by the slow EMA stack:
   * close > slow_ema  AND slow_ema_slope > +slope_threshold  -> LONG
   * close < slow_ema  AND slow_ema_slope < -slope_threshold  -> SHORT
   * else  -> NEUTRAL

2. **Regime** is set by the relationship between price, EMAs, and
   ATR:
   * If |close - slow_ema| / slow_ema > trend_distance_pct   -> TRENDING
   * Else if ATR_pct < range_atr_pct_max                     -> RANGING
   * Else                                                    -> VOLATILE

3. **Mode** is the strategy hint derived from regime + bias:
   * (trending, long)   -> trend_follow (long-only entries)
   * (trending, short)  -> trend_follow (short-only entries)
   * (ranging, *)       -> mean_revert  (fade extremes)
   * (volatile, *)      -> skip         (sit on hands)
   * (trending, neutral)-> skip         (slope flat, wait)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class HtfRegimeClassifierConfig:
    """Knobs for the HTF regime classifier."""

    # EMA periods. On daily bars: 50/200 = the classic.
    fast_ema: int = 50
    slow_ema: int = 200

    # Slope is computed over slope_lookback bars; threshold is the
    # required % change of the slow EMA over that window for a
    # trending bias.
    slope_lookback: int = 10
    slope_threshold_pct: float = 0.5

    # Trending vs ranging cutoff: how far from slow EMA (as % of EMA)
    # before we declare "trending" rather than "ranging".
    trend_distance_pct: float = 3.0

    # Ranging vs volatile cutoff: ATR as % of close. Below this, the
    # market is genuinely calm; above this it's volatile.
    range_atr_pct_max: float = 2.0

    # Window for ATR calculation
    atr_period: int = 14

    # Minimum bars to see before classifying — below this, return
    # ("neutral", "volatile", "skip") to fail-closed.
    warmup_bars: int = 220

    # ── Asymmetric hysteresis (anti-thrash) ──
    # Symmetric thresholds cause mode flicker on every bar near the
    # cutoff. When ALREADY in trend mode, apply a TIGHTER exit
    # threshold so the regime sticks: easier to STAY in trend than
    # to ENTER it.
    #
    # Defaults: slope must drop below 0.66 * slope_threshold_pct AND
    # ATR must rise above 1.5 * range_atr_pct_max before we leave
    # trend mode. Set to None to fall back to symmetric (legacy)
    # behaviour and use the entry thresholds.
    slope_threshold_exit: float | None = None
    atr_pct_max_exit: float | None = None


@dataclass
class HtfRegimeClassification:
    """The classifier's output for a single bar."""

    bias: str  # "long" | "short" | "neutral"
    regime: str  # "trending" | "ranging" | "volatile"
    mode: str  # "trend_follow" | "mean_revert" | "skip"
    # Audit: the raw values that produced the classification
    close: float = 0.0
    fast_ema: float = 0.0
    slow_ema: float = 0.0
    slope_pct: float = 0.0
    distance_from_slow_pct: float = 0.0
    atr_pct: float = 0.0
    components: dict[str, float] = field(default_factory=dict)


class HtfRegimeClassifier:
    """Stateful HTF regime classifier — one bar at a time.

    Maintain rolling EMAs + slope window + ATR. Caller calls
    ``update(bar)`` once per bar. ``classify(bar)`` returns the
    classification for the latest update — pass the same bar.
    The split makes warmup logic + audit cleaner.
    """

    def __init__(self, config: HtfRegimeClassifierConfig | None = None) -> None:
        self.cfg = config or HtfRegimeClassifierConfig()
        self._fast_ema: float | None = None
        self._slow_ema: float | None = None
        self._slow_ema_history: deque[float] = deque(
            maxlen=self.cfg.slope_lookback + 5,
        )
        self._atr_window: deque[tuple[float, float]] = deque(
            maxlen=self.cfg.atr_period,
        )
        self._bars_seen: int = 0
        self._fast_alpha = 2.0 / (self.cfg.fast_ema + 1)
        self._slow_alpha = 2.0 / (self.cfg.slow_ema + 1)
        # Track current mode for asymmetric hysteresis. We use the
        # trending-vs-not boolean (not the full mode string) since
        # only trend entry/exit needs the hysteresis treatment.
        self._currently_trending: bool = False
        # Resolved exit thresholds (None → tighter defaults derived
        # from entry thresholds when currently in trend).
        self._slope_exit = (
            self.cfg.slope_threshold_exit
            if self.cfg.slope_threshold_exit is not None
            else 0.66 * self.cfg.slope_threshold_pct
        )
        self._atr_exit = (
            self.cfg.atr_pct_max_exit if self.cfg.atr_pct_max_exit is not None else 1.5 * self.cfg.range_atr_pct_max
        )

    def update(self, bar: BarData) -> None:
        """Advance state by one bar. Always call this before classify()."""
        self._bars_seen += 1
        if self._fast_ema is None:
            self._fast_ema = bar.close
        else:
            self._fast_ema = self._fast_alpha * bar.close + (1 - self._fast_alpha) * self._fast_ema
        if self._slow_ema is None:
            self._slow_ema = bar.close
        else:
            self._slow_ema = self._slow_alpha * bar.close + (1 - self._slow_alpha) * self._slow_ema
        self._slow_ema_history.append(self._slow_ema)
        self._atr_window.append((bar.high, bar.low))

    def classify(self, bar: BarData) -> HtfRegimeClassification:
        """Compute (bias, regime, mode) for the current state.

        Always returns a valid classification; during warmup returns
        the safe ("neutral", "volatile", "skip") triple.
        """
        if self._bars_seen < self.cfg.warmup_bars or self._fast_ema is None or self._slow_ema is None:
            return HtfRegimeClassification(
                bias="neutral",
                regime="volatile",
                mode="skip",
                close=bar.close,
                fast_ema=self._fast_ema or 0.0,
                slow_ema=self._slow_ema or 0.0,
            )

        # ── Slope ──
        if len(self._slow_ema_history) < self.cfg.slope_lookback + 1:
            slope_pct = 0.0
        else:
            old = self._slow_ema_history[-self.cfg.slope_lookback - 1]
            slope_pct = (self._slow_ema - old) / max(old, 1e-9) * 100.0

        # ── Distance from slow EMA ──
        distance_pct = (bar.close - self._slow_ema) / max(self._slow_ema, 1e-9) * 100.0

        # ── ATR / close as % ──
        if not self._atr_window:
            atr_pct = 0.0
        else:
            atr = sum(h - low for h, low in self._atr_window) / len(self._atr_window)
            atr_pct = atr / max(bar.close, 1e-9) * 100.0

        # ── Bias ──
        # Asymmetric hysteresis: when CURRENTLY in trend mode, use
        # the looser exit thresholds (i.e. easier to STAY in trend
        # than to ENTER it). Prevents mode-thrash near the threshold.
        slope_thresh = self._slope_exit if self._currently_trending else self.cfg.slope_threshold_pct
        if distance_pct > 0 and slope_pct > slope_thresh:
            bias = "long"
        elif distance_pct < 0 and slope_pct < -slope_thresh:
            bias = "short"
        else:
            bias = "neutral"

        # ── Regime ──
        # Same hysteresis principle: when in trend mode, allow more
        # ATR before flipping to volatile (i.e. require atr_pct to
        # exceed atr_pct_max_exit, not just range_atr_pct_max).
        atr_thresh = self._atr_exit if self._currently_trending else self.cfg.range_atr_pct_max
        if abs(distance_pct) > self.cfg.trend_distance_pct:
            regime = "trending"
        elif atr_pct < atr_thresh:
            regime = "ranging"
        else:
            regime = "volatile"

        # ── Mode ──
        if regime == "volatile" or regime == "trending" and bias == "neutral":
            mode = "skip"
        elif regime == "trending":
            mode = "trend_follow"
        else:  # ranging
            mode = "mean_revert"

        # Update hysteresis state — track whether we're currently in
        # a trending regime so the next bar uses the right thresholds.
        self._currently_trending = regime == "trending"

        return HtfRegimeClassification(
            bias=bias,
            regime=regime,
            mode=mode,
            close=bar.close,
            fast_ema=self._fast_ema,
            slow_ema=self._slow_ema,
            slope_pct=slope_pct,
            distance_from_slow_pct=distance_pct,
            atr_pct=atr_pct,
            components={
                "fast_ema": self._fast_ema,
                "slow_ema": self._slow_ema,
                "slope_pct": slope_pct,
                "distance_pct": distance_pct,
                "atr_pct": atr_pct,
            },
        )
