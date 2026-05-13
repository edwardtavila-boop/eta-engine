"""
EVOLUTIONARY TRADING ALGO  //  strategies.htf_routed_strategy
===============================================================
HTF-routed multi-mode strategy: HTF classifier determines regime
+ bias, LTF execution dispatches to trend-follow OR mean-revert
mode based on the read.

User insight (2026-04-27): "higher time frame to determine current
time frame location... last month has been bearish, our strategy
should mainly be bearish with mean reversion fit for HTF and
directional being mainly HTF... after confirming regime we can
scalp lower time frames... using fibs and mean reversion or
liquidity concepts."

Architecture
------------
    HTF (daily) → HtfRegimeClassifier → (bias, regime, mode)
        │
        ▼
    LTF (1h)   → HtfRoutedStrategy → dispatches based on mode:
                    trend_follow  → CryptoRegimeTrendStrategy
                    mean_revert   → MeanRevertEntryStrategy
                    skip          → return None

The two LTF sub-strategies share the same exit machinery (ATR
stop / RR target / engine-managed), but use different ENTRY
triggers:

  * **trend_follow** mode: pullback to faster trend EMA in HTF
    bias direction. Long-only when HTF says long, short-only
    when HTF says short. Uses crypto_regime_trend.

  * **mean_revert** mode: fade extremes to the regime EMA midline
    when HTF is ranging. RSI / Bollinger touch + reversion
    bounce confirms the entry. Bidirectional within the range
    (not biased by HTF in the absence of a directional read).

The HTF classifier needs DAILY bars but the strategy receives
LTF (1h) bars from the engine. The classifier is fed via a
provider — the runner builds it from daily bars in advance and
attaches it. On each LTF bar, the strategy queries the provider
for the most recent classification at-or-before the LTF
timestamp.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from eta_engine.strategies.crypto_regime_trend_strategy import (
    CryptoRegimeTrendConfig,
    CryptoRegimeTrendStrategy,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData
    from eta_engine.strategies.htf_regime_classifier import HtfRegimeClassification


@dataclass(frozen=True)
class MeanRevertConfig:
    """Knobs for the mean-revert sub-strategy.

    Fires when bar's wick pierces the regime EMA by ≥
    `extreme_distance_pct` AND closes back inside the band
    (mean-revert continuation). Long when low pierces below,
    short when high pierces above.
    """

    regime_ema: int = 20
    # How far past the regime EMA the bar must wick before we
    # consider it an "extreme". Below this, no fire.
    extreme_distance_pct: float = 1.5

    # Risk / exits — mean-rev has tighter stop + smaller target
    # since the move-to-mean is shorter than a trend continuation.
    atr_period: int = 14
    atr_stop_mult: float = 1.0
    rr_target: float = 1.5
    risk_per_trade_pct: float = 0.01

    # Hygiene
    min_bars_between_trades: int = 6
    max_trades_per_day: int = 3
    warmup_bars: int = 30


def _ema_step(prev: float | None, value: float, period: int) -> float:
    if prev is None:
        return value
    alpha = 2.0 / (period + 1)
    return alpha * value + (1 - alpha) * prev


@dataclass(frozen=True)
class HtfRoutedConfig:
    """Combined config: HTF lookup + LTF sub-strategy configs."""

    # The HTF classification provider — receives the LTF bar's
    # timestamp and returns the most recent HTF classification
    # at-or-before that timestamp. Caller attaches it.
    # (Stored as runtime state on the strategy, not in this
    # frozen config.)

    # Sub-strategy configs
    trend_follow: CryptoRegimeTrendConfig = field(
        default_factory=CryptoRegimeTrendConfig,
    )
    mean_revert: MeanRevertConfig = field(default_factory=MeanRevertConfig)

    # When HTF mode is "trend_follow" but the classifier's bias
    # disagrees with the LTF entry side, hard-veto. Default ON.
    enforce_htf_bias_alignment: bool = True

    # When HTF mode is "skip" (volatile regime), we can either
    # honor the skip (default) or fall through to trend_follow.
    honor_htf_skip: bool = True


# ---------------------------------------------------------------------------
# Mean-revert sub-strategy
# ---------------------------------------------------------------------------


class MeanRevertSubStrategy:
    """Bidirectional mean-revert: bar wick pierces extreme + close
    back inside the band → fade move toward regime EMA midline.

    Used by HtfRoutedStrategy when HTF says "ranging". Could be
    used standalone but its math is intentionally minimal — the
    ranging-only constraint keeps it from fighting trends.
    """

    def __init__(self, config: MeanRevertConfig | None = None) -> None:
        self.cfg = config or MeanRevertConfig()
        self._regime_ema: float | None = None
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
        bar_date = bar.timestamp.date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0
        self._bars_seen += 1
        self._regime_ema = _ema_step(self._regime_ema, bar.close, self.cfg.regime_ema)

        if self._bars_seen < self.cfg.warmup_bars or self._regime_ema is None or len(hist) < self.cfg.atr_period + 1:
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx) < self.cfg.min_bars_between_trades
        ):
            return None

        # Mean-revert entry: bar wick pierces extreme distance from
        # regime EMA AND close is back inside the band.
        ext_pct = self.cfg.extreme_distance_pct / 100.0
        upper = self._regime_ema * (1.0 + ext_pct)
        lower = self._regime_ema * (1.0 - ext_pct)

        side: str | None = None
        if bar.low <= lower and bar.close > lower:
            side = "BUY"  # piercing below extreme + closing back up = fade short
        elif bar.high >= upper and bar.close < upper:
            side = "SELL"  # piercing above extreme + closing back down = fade long

        if side is None:
            return None

        # ATR sizing
        atr_window = hist[-self.cfg.atr_period :] if hist else []
        atr = sum(b.high - b.low for b in atr_window) / max(len(atr_window), 1)
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
            regime="htf_routed_mean_revert",
        )
        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        return opened


# ---------------------------------------------------------------------------
# Router strategy
# ---------------------------------------------------------------------------


class HtfRoutedStrategy:
    """Reads the HTF classification, dispatches to mode-appropriate
    LTF sub-strategy, and applies the HTF bias as a directional
    veto.

    Both sub-strategies receive the LTF bar stream. The router
    only fires when HTF mode is non-skip; the chosen sub-strategy
    proposes the entry; HTF bias filters the side.
    """

    def __init__(self, config: HtfRoutedConfig | None = None) -> None:
        self.cfg = config or HtfRoutedConfig()
        self._trend = CryptoRegimeTrendStrategy(self.cfg.trend_follow)
        self._mean_revert = MeanRevertSubStrategy(self.cfg.mean_revert)
        # HTF classification provider: callable(bar) -> HtfRegimeClassification
        self._htf_provider: Callable[[BarData], HtfRegimeClassification] | None = None
        self._last_classification: HtfRegimeClassification | None = None
        self._mode_history: deque[str] = deque(maxlen=100)

    def attach_htf_classification_provider(
        self,
        provider: Callable[[BarData], HtfRegimeClassification] | None,
    ) -> None:
        """Wire up the HTF classification source.

        The provider takes the LTF bar and returns the most recent
        HTF classification at-or-before that bar's timestamp.
        """
        self._htf_provider = provider

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        # Always advance both sub-strategies' state (so EMAs stay current)
        # by calling them — we discard the unused side's output.
        trend_proposal = self._trend.maybe_enter(bar, hist, equity, config)
        mr_proposal = self._mean_revert.maybe_enter(bar, hist, equity, config)

        # No HTF provider attached → fail-closed (don't trade blind)
        if self._htf_provider is None:
            self._rollback(trend_proposal, mr_proposal)
            return None

        try:
            cls = self._htf_provider(bar)
        except Exception:  # noqa: BLE001 - provider isolation
            self._rollback(trend_proposal, mr_proposal)
            return None

        self._last_classification = cls
        self._mode_history.append(cls.mode)

        # Skip mode → both sub-strategies' proposals get rolled back
        if cls.mode == "skip" and self.cfg.honor_htf_skip:
            self._rollback(trend_proposal, mr_proposal)
            return None

        # Dispatch to the mode-appropriate proposal
        chosen = trend_proposal if cls.mode == "trend_follow" else mr_proposal
        unchosen = mr_proposal if cls.mode == "trend_follow" else trend_proposal

        # Roll back the unchosen proposal regardless
        self._rollback_one(unchosen, is_trend=(cls.mode != "trend_follow"))

        if chosen is None:
            return None

        # HTF bias enforcement (only for trend_follow mode)
        if cls.mode == "trend_follow" and self.cfg.enforce_htf_bias_alignment and cls.bias != "neutral":
            htf_side = "BUY" if cls.bias == "long" else "SELL"
            if chosen.side != htf_side:
                self._rollback_one(chosen, is_trend=True)
                return None

        # Tag regime so the audit trail captures the route
        new_tag = f"htf_routed_{cls.mode}_{cls.bias}_{cls.regime}"
        return replace(chosen, regime=new_tag)

    # -- helpers --------------------------------------------------------------

    def _rollback(self, trend: object, mr: object) -> None:  # noqa: ANN001
        """Roll back both sub-strategies' cooldowns when neither fires."""
        self._rollback_one(trend, is_trend=True)
        self._rollback_one(mr, is_trend=False)

    def _rollback_one(self, opened: object, *, is_trend: bool) -> None:  # noqa: ANN001
        if opened is None:
            return
        s = self._trend if is_trend else self._mean_revert
        s._trades_today = max(0, s._trades_today - 1)
        cooldown = (
            self.cfg.trend_follow.min_bars_between_trades if is_trend else self.cfg.mean_revert.min_bars_between_trades
        )
        if s._last_entry_idx is not None:
            s._last_entry_idx = s._bars_seen - cooldown - 1
