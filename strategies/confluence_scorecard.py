"""
EVOLUTIONARY TRADING ALGO  //  strategies.confluence_scorecard
=================================================================
Confluence scoring wrapper — applies the user-spec'd 3-of-5 /
4-5 A+ scorecard to any sub-strategy.

User mandate (2026-04-27):
"Use a scorecard, not a pile of indicators. A setup must have
the main trigger first. Then score confluence. Minimum score to
enter = 3 out of 5. A+ setup = 4 or 5 out of 5."

Mechanic
--------
The wrapper does NOT generate signals — it gates them.

Flow per bar:
1. Sub-strategy proposes an entry (its own internal trigger).
2. Wrapper computes a 0-N confluence score from SEPARATE checks
   on the SAME bar:
   * Trend alignment (EMA 9 / 21 / 50 stack)
   * VWAP alignment (price vs VWAP)
   * Higher-timeframe agreement (caller-supplied predicate)
   * Volatility regime (ATR percentile in [min, max])
   * Volume confirmation (volume z-score above min)
   * Liquidity proximity (caller-supplied: was a key level just touched)
   * Time-of-day (caller-supplied session predicate)
3. If score < min_score → veto.
4. If score >= a_plus_score → tag as A+ in the trade's regime
   field, and (optionally) increase position size.

Distinct from RegimeGatedStrategy
---------------------------------
RegimeGatedStrategy is a HARD GATE based on a single
classification (regime label). ConfluenceScorecard is a SOFT
GATE based on a sum-of-factors score. They compose:

    raw_strategy
      → RegimeGatedStrategy (regime must allow trading)
      → ConfluenceScorecard (factor count must hit threshold)
      → AdaptiveKellySizing (sizing scales with streak)

Each layer is generic, opt-in, and stackable.

A+ size boost
-------------
When score >= a_plus_score, optionally multiply the proposed
qty by a_plus_size_mult (default 1.5). This is the "increase
size only if score >= 4" rule from the user spec, mechanized.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Protocol

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData

    class _SubStrategy(Protocol):
        def maybe_enter(
            self,
            bar: BarData,
            hist: list[BarData],
            equity: float,
            config: BacktestConfig,
        ) -> _Open | None: ...


@dataclass(frozen=True)
class ConfluenceScorecardConfig:
    """Knobs for the confluence scorecard."""

    # Minimum score to allow a trade through. Default 3 = 3-of-N
    # factors required.
    min_score: int = 3
    # Score threshold for "A+" tagging + size boost. Default 4.
    a_plus_score: int = 4
    # Position-size multiplier when A+ qualifies. 1.0 = no boost.
    a_plus_size_mult: float = 1.5

    # Trend-stack EMA periods (0 = disabled)
    fast_ema: int = 9
    mid_ema: int = 21
    slow_ema: int = 50
    enable_trend_factor: bool = True

    # VWAP factor — uses session-anchored VWAP (resets daily)
    enable_vwap_factor: bool = True

    # Volatility-regime factor — ATR percentile band
    atr_period: int = 14
    atr_pct_lookback: int = 100
    atr_pct_min: float = 0.20
    atr_pct_max: float = 0.80
    enable_atr_regime_factor: bool = True

    # Volume z-score factor
    volume_z_lookback: int = 20
    volume_z_min: float = 0.30
    enable_volume_factor: bool = True

    # Tag the trade with score for audit
    tag_score: bool = True


def _ema_step(prev: float | None, value: float, period: int) -> float:
    if prev is None:
        return value
    alpha = 2.0 / (period + 1)
    return alpha * value + (1 - alpha) * prev


class ConfluenceScorecardStrategy:
    """Wraps a sub-strategy with a 0-N confluence-factor scorecard.

    Usage:
        sub = SweepReclaimStrategy(...)
        scored = ConfluenceScorecardStrategy(sub, ConfluenceScorecardConfig(...))
        # optional: scored.attach_htf_agreement(htf_predicate)
        # optional: scored.attach_session_predicate(session_predicate)
    """

    def __init__(
        self,
        sub_strategy: _SubStrategy,
        config: ConfluenceScorecardConfig | None = None,
    ) -> None:
        self._sub = sub_strategy
        self.cfg = config or ConfluenceScorecardConfig()
        # Internal state for factor evaluation
        self._fast_ema: float | None = None
        self._mid_ema: float | None = None
        self._slow_ema: float | None = None
        self._vwap_pv: float = 0.0  # cumulative price*volume (session)
        self._vwap_v: float = 0.0  # cumulative volume (session)
        self._vwap_day: object | None = None
        self._tr_window: deque[float] = deque(
            maxlen=self.cfg.atr_pct_lookback + 5,
        )
        self._volume_window: deque[float] = deque(
            maxlen=self.cfg.volume_z_lookback,
        )
        # External predicates
        self._htf_predicate: Callable[[BarData, str], bool] | None = None
        self._session_predicate: Callable[[BarData], bool] | None = None
        self._liquidity_predicate: Callable[[BarData, str], bool] | None = None
        # Audit
        self._n_proposed: int = 0
        self._n_vetoed: int = 0
        self._n_a_plus: int = 0
        self._score_distribution: dict[int, int] = {}

    # -- predicate plumbing -------------------------------------------------

    def attach_htf_agreement(
        self,
        predicate: Callable[[BarData, str], bool] | None,
    ) -> None:
        """Attach a higher-timeframe agreement predicate.
        Signature: ``predicate(bar, side) -> bool``.
        Returns True if HTF agrees with the proposed side."""
        self._htf_predicate = predicate

    def attach_session_predicate(
        self,
        predicate: Callable[[BarData], bool] | None,
    ) -> None:
        """Attach a high-liquidity-session predicate.
        Returns True if bar timestamp is in a desirable session window."""
        self._session_predicate = predicate

    def attach_liquidity_predicate(
        self,
        predicate: Callable[[BarData, str], bool] | None,
    ) -> None:
        """Attach a liquidity-level proximity predicate.
        Returns True if a key level (PDH/PDL/VWAP/round-number) was
        recently swept or reclaimed in the trade direction."""
        self._liquidity_predicate = predicate

    # -- audit -------------------------------------------------------------

    @property
    def scorecard_stats(self) -> dict[str, int | dict[int, int]]:
        return {
            "proposed": self._n_proposed,
            "vetoed": self._n_vetoed,
            "a_plus_fires": self._n_a_plus,
            "score_distribution": dict(self._score_distribution),
        }

    # -- factor evaluators --------------------------------------------------

    def _trend_factor(self, bar: BarData, side: str) -> int:
        """+1 if EMA stack agrees with side; 0 otherwise."""
        if not self.cfg.enable_trend_factor:
            return 0
        if self._fast_ema is None or self._mid_ema is None or self._slow_ema is None:
            return 0
        if side == "BUY" and self._fast_ema > self._mid_ema > self._slow_ema and bar.close > self._fast_ema:
            return 1
        if side == "SELL" and self._fast_ema < self._mid_ema < self._slow_ema and bar.close < self._fast_ema:
            return 1
        return 0

    def _vwap_factor(self, bar: BarData, side: str) -> int:
        """+1 if price aligned with session VWAP; 0 otherwise."""
        if not self.cfg.enable_vwap_factor:
            return 0
        if self._vwap_v <= 0.0:
            return 0
        vwap = self._vwap_pv / self._vwap_v
        if side == "BUY" and bar.close > vwap:
            return 1
        if side == "SELL" and bar.close < vwap:
            return 1
        return 0

    def _atr_regime_factor(self) -> int:
        """+1 if current ATR percentile in [min, max] band."""
        if not self.cfg.enable_atr_regime_factor:
            return 0
        if len(self._tr_window) < self.cfg.atr_period:
            return 0
        recent_tr = list(self._tr_window)[-self.cfg.atr_period :]
        atr = sum(recent_tr) / len(recent_tr)
        if len(self._tr_window) < self.cfg.atr_pct_lookback:
            return 0
        sorted_tr = sorted(self._tr_window)
        # Find percentile of current ATR
        rank = sum(1 for v in sorted_tr if v <= atr)
        pct = rank / len(sorted_tr)
        if self.cfg.atr_pct_min <= pct <= self.cfg.atr_pct_max:
            return 1
        return 0

    def _volume_factor(self, bar: BarData) -> int:
        """+1 if bar volume z-score >= min."""
        if not self.cfg.enable_volume_factor:
            return 0
        if len(self._volume_window) < self.cfg.volume_z_lookback:
            return 0
        vols = list(self._volume_window)
        mean = sum(vols) / len(vols)
        var = sum((v - mean) ** 2 for v in vols) / len(vols)
        std = var**0.5
        if std <= 0.0:
            return 0
        z = (bar.volume - mean) / std
        return 1 if z >= self.cfg.volume_z_min else 0

    def _htf_factor(self, bar: BarData, side: str) -> int:
        """+1 if HTF predicate agrees with side."""
        if self._htf_predicate is None:
            return 0
        try:
            return 1 if self._htf_predicate(bar, side) else 0
        except Exception:  # noqa: BLE001 - predicate isolation
            return 0

    def _session_factor(self, bar: BarData) -> int:
        """+1 if current bar in a high-liquidity session."""
        if self._session_predicate is None:
            return 0
        try:
            return 1 if self._session_predicate(bar) else 0
        except Exception:  # noqa: BLE001
            return 0

    def _liquidity_factor(self, bar: BarData, side: str) -> int:
        """+1 if liquidity-level predicate agrees with side."""
        if self._liquidity_predicate is None:
            return 0
        try:
            return 1 if self._liquidity_predicate(bar, side) else 0
        except Exception:  # noqa: BLE001
            return 0

    # -- main entry point --------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        # Update internal indicators on EVERY bar (so factors are
        # accurate when sub-strategy fires).
        self._fast_ema = _ema_step(self._fast_ema, bar.close, self.cfg.fast_ema)
        self._mid_ema = _ema_step(self._mid_ema, bar.close, self.cfg.mid_ema)
        self._slow_ema = _ema_step(self._slow_ema, bar.close, self.cfg.slow_ema)
        # Reset session VWAP at day boundary
        d = bar.timestamp.date()
        if self._vwap_day != d:
            self._vwap_pv = 0.0
            self._vwap_v = 0.0
            self._vwap_day = d
        typical = (bar.high + bar.low + bar.close) / 3.0
        self._vwap_pv += typical * bar.volume
        self._vwap_v += bar.volume
        # True range
        if hist:
            prev_close = hist[-1].close
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
        else:
            tr = bar.high - bar.low
        self._tr_window.append(tr)
        self._volume_window.append(bar.volume)

        # Always advance underlying state
        opened = self._sub.maybe_enter(bar, hist, equity, config)
        if opened is None:
            return None

        self._n_proposed += 1

        # Compute score
        side = opened.side
        score = (
            self._trend_factor(bar, side)
            + self._vwap_factor(bar, side)
            + self._atr_regime_factor()
            + self._volume_factor(bar)
            + self._htf_factor(bar, side)
            + self._session_factor(bar)
            + self._liquidity_factor(bar, side)
        )
        self._score_distribution[score] = self._score_distribution.get(score, 0) + 1

        if score < self.cfg.min_score:
            self._n_vetoed += 1
            return None

        # A+ size boost
        if score >= self.cfg.a_plus_score and self.cfg.a_plus_size_mult > 1.0:
            self._n_a_plus += 1
            scaled_qty = opened.qty * self.cfg.a_plus_size_mult
            scaled_risk = opened.risk_usd * self.cfg.a_plus_size_mult
            new_tag = f"{opened.regime}_score{score}_aplus" if self.cfg.tag_score else opened.regime
            return replace(
                opened,
                qty=scaled_qty,
                risk_usd=scaled_risk,
                regime=new_tag,
            )

        if self.cfg.tag_score:
            new_tag = f"{opened.regime}_score{score}"
            return replace(opened, regime=new_tag)
        return opened
