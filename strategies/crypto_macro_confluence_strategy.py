"""
EVOLUTIONARY TRADING ALGO  //  strategies.crypto_macro_confluence_strategy
==========================================================================
Macro-confluence wrapper around crypto_regime_trend.

User insight (2026-04-27, follow-up): "no one factor truly moves
[BTC] alone — they interact." The factors the user listed:
ETF flows, macro liquidity, leverage / funding, on-chain LTH
supply, sentiment, time-of-day session.

This strategy implements a stackable-filter architecture: a base
regime-trend signal that produces candidate entries, layered with
opt-in confluence filters. Each filter is independently toggled
in config and walk-forward-tested in the sweep. The "perfect edge"
isn't a single rule — it's the right combination of independent
gates.

Filter inventory (and which have working data sources today)
------------------------------------------------------------

| Filter                    | Data       | Status   |
|---------------------------|------------|----------|
| HTF EMA alignment         | OHLCV      | active   |
| Time-of-day window        | OHLCV ts   | active   |
| Volatility regime band    | OHLCV ATR  | active   |
| BTC-ETH correlation       | BTC + ETH  | active   |
| Funding rate not extreme  | BTCFUND_8h | active   |
| Macro tailwind (DXY/SPY)  | DXY + SPY  | needs fetcher |
| ETF flow positive         | IBIT       | needs fetcher (Tier 4) |
| On-chain LTH supply       | exchange   | needs fetcher (Tier 4) |

The strategy reads optional providers (callables that return a
score for the current bar) for each non-OHLCV-derivable filter.
Providers are attached at runner setup. None attached → that
filter is a no-op. This keeps the strategy data-pipeline-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from eta_engine.strategies.crypto_regime_trend_strategy import (
    CryptoRegimeTrendConfig,
    CryptoRegimeTrendStrategy,
)

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class MacroConfluenceConfig:
    """Filter knobs. Each filter is opt-in via a non-default value."""

    # ── A. HTF EMA alignment ──
    # Require a SECOND, slower EMA on the same bar stream to also
    # be on the same regime side. On 1h, htf_ema=800 ≈ 33 daily
    # bars ≈ a "monthly cycle" gate. 0 = disabled.
    htf_ema_period: int = 0

    # ── B. Time-of-day window (UTC hours) ──
    # When non-empty, ONLY fire entries when the bar's UTC hour is
    # in this set. Default empty = all hours allowed.
    # Recommended for BTC: {13, 14, 15, 16} = London/NY overlap.
    allow_utc_hours: frozenset[int] = field(default_factory=frozenset)

    # ── C. Volatility regime band (ATR percentile) ──
    # Skip when ATR is in the bottom or top decile of its rolling
    # window — extremes are chop or panic respectively. 0 = disabled.
    vol_band_lookback: int = 0
    vol_band_min_pct: float = 0.10
    vol_band_max_pct: float = 0.90

    # ── D. BTC-ETH correlation gate ──
    # Provider returns 1.0 (eth aligned same direction), -1.0 (eth
    # opposite), 0.0 (eth neutral / no data). Strategy fires only
    # when score >= min_correlation_score. None = disabled.
    require_eth_alignment: bool = False

    # ── E. Funding-rate filter ──
    # Provider returns the current funding rate. Strategy skips
    # when |funding| > extreme_funding_threshold (overheated longs
    # or shorts that historically mean-revert). None = disabled.
    extreme_funding_threshold: float = 0.0  # 0 = disabled; 0.001 = 0.1%

    # ── F. Macro tailwind (DXY weak + SPY trending) ──
    # Provider returns a composite score in [-1, +1]. Required
    # alignment: long needs score > min_macro_score, short needs
    # score < -min_macro_score. 0 = disabled.
    min_macro_score: float = 0.0

    # ── G. ETF flow positive (Tier 4 - placeholder) ──
    # Provider returns daily net BTC flow. Long requires positive,
    # short requires negative. 0 = disabled.
    require_etf_flow_alignment: bool = False

    # ── H. On-chain LTH supply (Tier 4 - active) ──
    # Provider returns LTH proxy in [-1, +1] (Mayer Multiple
    # percentile-derived). +1 = accumulation phase. Long requires
    # score > min_lth_score, short requires score < -min_lth_score.
    # 0 = disabled.
    min_lth_score: float = 0.0

    # ── I. Sentiment (Tier 4 - active) ──
    # Provider returns Fear & Greed normalized to [-1, +1]
    # (CONTRARIAN: fear = +1, greed = -1). Long requires score >=
    # min_sentiment_score (i.e. enough fear / not too greedy),
    # short requires score <= -min_sentiment_score (i.e. enough
    # greed / not too fearful). 0 = disabled.
    min_sentiment_score: float = 0.0

    # Legacy on-chain alignment toggle kept for back-compat with
    # tests that referenced it before the LTH proxy landed. Behaves
    # identically to ``min_lth_score > 0``.
    require_onchain_alignment: bool = False


@dataclass(frozen=True)
class CryptoMacroConfluenceConfig:
    """Combined base + filter config."""

    base: CryptoRegimeTrendConfig = field(default_factory=CryptoRegimeTrendConfig)
    filters: MacroConfluenceConfig = field(default_factory=MacroConfluenceConfig)


# ---------------------------------------------------------------------------
# Provider type aliases (each takes the current bar, returns a float score)
# ---------------------------------------------------------------------------

if TYPE_CHECKING:
    EthAlignmentProvider = Callable[["BarData"], float]
    FundingRateProvider = Callable[["BarData"], float]
    MacroTailwindProvider = Callable[["BarData"], float]
    EtfFlowProvider = Callable[["BarData"], float]
    OnchainProvider = Callable[["BarData"], float]


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


def _ema_step(prev: float | None, value: float, period: int) -> float:
    if prev is None:
        return value
    alpha = 2.0 / (period + 1)
    return alpha * value + (1 - alpha) * prev


class CryptoMacroConfluenceStrategy:
    """Regime-trend with stackable confluence filters.

    Composition pattern: embeds a CryptoRegimeTrendStrategy and
    delegates the candidate-entry logic. When the base strategy
    returns an _Open, we run each enabled filter against the bar.
    Any veto returns None and rolls back the base strategy's
    cooldown so a later bar with confluence can still fire.
    """

    def __init__(self, config: CryptoMacroConfluenceConfig | None = None) -> None:
        self.cfg = config or CryptoMacroConfluenceConfig()
        self._base = CryptoRegimeTrendStrategy(self.cfg.base)
        self._htf_ema: float | None = None
        self._atr_history: list[float] = []
        # Filter providers — attach via setters before running
        self._eth_provider: Callable[[BarData], float] | None = None
        self._funding_provider: Callable[[BarData], float] | None = None
        self._macro_provider: Callable[[BarData], float] | None = None
        self._etf_provider: Callable[[BarData], float] | None = None
        self._onchain_provider: Callable[[BarData], float] | None = None
        self._sentiment_provider: Callable[[BarData], float] | None = None
        # Track per-bar state so filters can read what the wrapper
        # already computed
        self._last_bar_atr: float | None = None

    # -- provider attachment --------------------------------------------------

    def attach_eth_alignment_provider(
        self, p: Callable[[BarData], float] | None,
    ) -> None:
        self._eth_provider = p

    def attach_funding_provider(
        self, p: Callable[[BarData], float] | None,
    ) -> None:
        self._funding_provider = p

    def attach_macro_provider(
        self, p: Callable[[BarData], float] | None,
    ) -> None:
        self._macro_provider = p

    def attach_etf_flow_provider(
        self, p: Callable[[BarData], float] | None,
    ) -> None:
        self._etf_provider = p

    def attach_onchain_provider(
        self, p: Callable[[BarData], float] | None,
    ) -> None:
        self._onchain_provider = p

    def attach_sentiment_provider(
        self, p: Callable[[BarData], float] | None,
    ) -> None:
        self._sentiment_provider = p

    # -- main entry point -----------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        # Update HTF EMA before delegating (filter A needs it)
        if self.cfg.filters.htf_ema_period > 0:
            self._htf_ema = _ema_step(
                self._htf_ema, bar.close, self.cfg.filters.htf_ema_period,
            )

        # Track ATR history for the volatility-regime filter
        if self.cfg.filters.vol_band_lookback > 0:
            atr_window = hist[-self.cfg.base.atr_period:] if hist else []
            if len(atr_window) >= 2:
                atr = sum(b.high - b.low for b in atr_window) / len(atr_window)
                self._atr_history.append(atr)
                # Cap history
                max_len = self.cfg.filters.vol_band_lookback + 50
                if len(self._atr_history) > max_len:
                    self._atr_history = self._atr_history[-max_len:]

        # Delegate the base candidate-entry decision
        opened = self._base.maybe_enter(bar, hist, equity, config)
        if opened is None:
            return None

        # Run each enabled filter; any veto → None + rollback base
        if not self._filter_htf_alignment(opened):
            self._rollback_base()
            return None
        if not self._filter_time_of_day(bar):
            self._rollback_base()
            return None
        if not self._filter_volatility_regime():
            self._rollback_base()
            return None
        if not self._filter_eth_alignment(bar, opened):
            self._rollback_base()
            return None
        if not self._filter_funding(bar, opened):
            self._rollback_base()
            return None
        if not self._filter_macro_tailwind(bar, opened):
            self._rollback_base()
            return None
        if not self._filter_etf_flow(bar, opened):
            self._rollback_base()
            return None
        if not self._filter_onchain(bar, opened):
            self._rollback_base()
            return None
        if not self._filter_sentiment(bar, opened):
            self._rollback_base()
            return None

        # All filters passed — annotate regime to record confluence
        from dataclasses import replace
        return replace(opened, regime=opened.regime + "_macro_conf")

    # -- filter implementations -----------------------------------------------

    def _filter_htf_alignment(self, opened: _Open) -> bool:
        """Variant A. Long requires close > htf_ema AND base regime ≥;
        short requires close < htf_ema AND base regime ≤."""
        if self.cfg.filters.htf_ema_period <= 0:
            return True
        if self._htf_ema is None:
            return True  # warmup — fail-open
        close = opened.entry_price
        if opened.side == "BUY":
            return close > self._htf_ema
        return close < self._htf_ema

    def _filter_time_of_day(self, bar: BarData) -> bool:
        """Variant B. Only fire when bar's UTC hour is in allow set."""
        if not self.cfg.filters.allow_utc_hours:
            return True
        return bar.timestamp.hour in self.cfg.filters.allow_utc_hours

    def _filter_volatility_regime(self) -> bool:
        """Variant C. Skip when ATR percentile is too low (chop) or
        too high (panic) within the rolling window."""
        if self.cfg.filters.vol_band_lookback <= 0:
            return True
        history = self._atr_history[-self.cfg.filters.vol_band_lookback:]
        if len(history) < self.cfg.filters.vol_band_lookback // 2:
            return True  # warmup — fail-open
        sorted_h = sorted(history)
        current = history[-1]
        # Find current's percentile via index in sorted history
        try:
            rank = sorted_h.index(current)
        except ValueError:
            return True
        pct = rank / max(len(sorted_h) - 1, 1)
        return self.cfg.filters.vol_band_min_pct <= pct <= self.cfg.filters.vol_band_max_pct

    def _filter_eth_alignment(self, bar: BarData, opened: _Open) -> bool:
        """Variant D. Provider returns +1 if ETH aligned, -1 opposite, 0 unknown."""
        if not self.cfg.filters.require_eth_alignment:
            return True
        if self._eth_provider is None:
            return False  # filter on but no provider → fail-closed
        try:
            score = self._eth_provider(bar)
        except Exception:  # noqa: BLE001
            return False
        # Long requires score > 0 (ETH same direction)
        if opened.side == "BUY":
            return score > 0.0
        return score < 0.0

    def _filter_funding(self, bar: BarData, opened: _Open) -> bool:
        """Variant E. Skip extreme funding (overheated longs / shorts)."""
        if self.cfg.filters.extreme_funding_threshold <= 0.0:
            return True
        if self._funding_provider is None:
            return True  # filter requested but no data — fail-open
        try:
            funding = self._funding_provider(bar)
        except Exception:  # noqa: BLE001
            return True
        # If funding is extremely positive (longs paying shorts), longs
        # are crowded → block new longs. Mirror for shorts.
        threshold = self.cfg.filters.extreme_funding_threshold
        if opened.side == "BUY" and funding > threshold:
            return False
        return not (opened.side == "SELL" and funding < -threshold)

    def _filter_macro_tailwind(self, bar: BarData, opened: _Open) -> bool:
        """Variant F. DXY weak + SPY trending = positive macro for BTC."""
        if self.cfg.filters.min_macro_score <= 0.0:
            return True
        if self._macro_provider is None:
            return True  # fail-open
        try:
            score = self._macro_provider(bar)
        except Exception:  # noqa: BLE001
            return True
        if opened.side == "BUY":
            return score >= self.cfg.filters.min_macro_score
        return score <= -self.cfg.filters.min_macro_score

    def _filter_etf_flow(self, bar: BarData, opened: _Open) -> bool:
        """Variant G. Long requires positive net ETF inflow."""
        if not self.cfg.filters.require_etf_flow_alignment:
            return True
        if self._etf_provider is None:
            return True  # fail-open until fetcher exists
        try:
            flow = self._etf_provider(bar)
        except Exception:  # noqa: BLE001
            return True
        return (opened.side == "BUY" and flow > 0) or (opened.side == "SELL" and flow < 0)

    def _filter_onchain(self, bar: BarData, opened: _Open) -> bool:
        """Variant H. LTH proxy in [-1, +1]: +1 = strong accumulation,
        -1 = strong distribution.

        Long requires score >= min_lth_score (or, back-compat,
        require_onchain_alignment=True with score>0).
        Short requires score <= -min_lth_score (or score<0 in back-
        compat mode).
        """
        threshold = self.cfg.filters.min_lth_score
        legacy = self.cfg.filters.require_onchain_alignment
        if threshold <= 0.0 and not legacy:
            return True
        if self._onchain_provider is None:
            return True  # fail-open
        try:
            score = self._onchain_provider(bar)
        except Exception:  # noqa: BLE001
            return True
        if legacy and threshold <= 0.0:
            return (
                (opened.side == "BUY" and score > 0)
                or (opened.side == "SELL" and score < 0)
            )
        if opened.side == "BUY":
            return score >= threshold
        return score <= -threshold

    def _filter_sentiment(self, bar: BarData, opened: _Open) -> bool:
        """Variant I. Fear & Greed normalized to [-1, +1] CONTRARIAN
        (fear=+1, greed=-1).

        Long requires score >= min_sentiment_score (enough fear /
        not too greedy). Short requires score <= -min_sentiment_score.
        """
        threshold = self.cfg.filters.min_sentiment_score
        if threshold <= 0.0:
            return True
        if self._sentiment_provider is None:
            return True  # fail-open
        try:
            score = self._sentiment_provider(bar)
        except Exception:  # noqa: BLE001
            return True
        if opened.side == "BUY":
            return score >= threshold
        return score <= -threshold

    # -- helpers --------------------------------------------------------------

    def _rollback_base(self) -> None:
        """Roll back the base strategy's last-entry latch + trade
        count so the same day can still fire later if confluence
        improves. Mirrors SageGatedORBStrategy's cooldown rollback.
        """
        # Decrement trades_today
        self._base._trades_today = max(0, self._base._trades_today - 1)
        # Reset last_entry_idx to one bar before so cooldown is
        # cleared instantly. This is intentional — the entry didn't
        # actually happen, so the cooldown shouldn't apply.
        if self._base._last_entry_idx is not None:
            self._base._last_entry_idx = (
                self._base._bars_seen - self.cfg.base.min_bars_between_trades - 1
            )
