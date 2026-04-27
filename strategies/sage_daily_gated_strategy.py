"""
EVOLUTIONARY TRADING ALGO  //  strategies.sage_daily_gated_strategy
====================================================================
Sage-daily-gated strategy: 1h crypto_macro_confluence + sage's
DAILY composite as the directional veto.

Why this is novel
-----------------
The user's insight (2026-04-27): Tier-4 / sage signals belong on
their natural HTF cadence, not as 1h overlays.

We've tested:
* Sage on every 1h bar (too noisy — blew up on BTC's low trade
  count)
* HTF classifier on daily (pure price-derived; 5 EMAs)

We HAVE NOT tested:
* Sage at the DAILY level as a directional gate.

The sage layer ships 22 schools (Dow, Wyckoff, Elliott, Fib, Gann,
SMC/ICT, order flow, NEoWave, Weis/Wyckoff, seasonality, vol regime,
options greeks, on-chain, funding, cross-asset corr, ML, sentiment,
plus more). On DAILY bars, the composite represents a multi-school
read at the same cadence as the macro drivers (ETF flows, LTH phase,
F&G index). That's a different — and uncorrelated — signal from
the price-EMA HTF classifier.

This strategy:

  1. Pre-computes sage's composite + bias for every BTC daily bar
     once at startup (single sage pass over the daily history).
  2. Wraps the +4.28 OOS ``crypto_regime_trend + ETF flow filter``
     champion strategy.
  3. On each 1h LTF bar, looks up the most recent daily sage read.
  4. Long requires sage's daily bias to be 'long' (or neutral).
     Short requires 'short' (or neutral). Strong-disagreement
     vetoes the entry.

Conviction is also exposed: an operator can require sage's daily
conviction to exceed a threshold for the gate to apply (otherwise
fall through to the underlying strategy unchanged).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from eta_engine.strategies.crypto_macro_confluence_strategy import (
    CryptoMacroConfluenceConfig,
    CryptoMacroConfluenceStrategy,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class SageDailyVerdict:
    """Daily sage read: composite bias + conviction."""

    direction: str          # 'long' | 'short' | 'neutral'
    conviction: float       # 0.0 - 1.0
    composite: float        # -1.0 to +1.0


@dataclass(frozen=True)
class SageDailyGatedConfig:
    """Combined config: underlying confluence strategy + sage gate."""

    base: CryptoMacroConfluenceConfig = field(
        default_factory=CryptoMacroConfluenceConfig,
    )

    # Minimum daily-sage conviction required for the gate to apply.
    # Below this, the underlying strategy fires unchanged (sage is
    # too uncertain to veto). Above, sage's direction must agree.
    min_daily_conviction: float = 0.30

    # Strict mode: require strict sage agreement (long-only when
    # sage says long, etc.). When False, neutral sage is allowed
    # (only opposite-direction sage vetoes).
    strict_mode: bool = False


class SageDailyGatedStrategy:
    """1h confluence strategy gated on sage's daily directional read.

    Composition:
      embed CryptoMacroConfluenceStrategy as the LTF executor;
      attach a daily-sage-verdict provider; on each 1h bar, look
      up most-recent daily verdict at-or-before bar.timestamp;
      veto trades that disagree with sage's directional bias when
      conviction >= min_daily_conviction.
    """

    def __init__(self, config: SageDailyGatedConfig | None = None) -> None:
        self.cfg = config or SageDailyGatedConfig()
        self._base = CryptoMacroConfluenceStrategy(self.cfg.base)
        self._verdict_provider: Callable[[date], SageDailyVerdict] | None = None

    # -- provider plumbing ---------------------------------------------------

    def attach_daily_verdict_provider(
        self, provider: Callable[[date], SageDailyVerdict] | None,
    ) -> None:
        """Attach a daily-sage verdict lookup.

        ``provider(date) -> SageDailyVerdict`` returns the sage
        composite for that calendar date (or the most recent
        daily date <= it). Caller pre-computes the table.
        """
        self._verdict_provider = provider

    # Forward provider attachments to the embedded macro confluence
    # strategy so callers can wire the underlying ETF/LTH/etc. once
    # at construction time.

    def attach_etf_flow_provider(self, p: object) -> None:  # noqa: ANN001
        self._base.attach_etf_flow_provider(p)

    def attach_lth_provider(self, p: object) -> None:  # noqa: ANN001
        self._base.attach_onchain_provider(p)

    def attach_fear_greed_provider(self, p: object) -> None:  # noqa: ANN001
        self._base.attach_sentiment_provider(p)

    def attach_macro_provider(self, p: object) -> None:  # noqa: ANN001
        self._base.attach_macro_provider(p)

    def attach_eth_alignment_provider(self, p: object) -> None:  # noqa: ANN001
        self._base.attach_eth_alignment_provider(p)

    def attach_funding_provider(self, p: object) -> None:  # noqa: ANN001
        self._base.attach_funding_provider(p)

    # -- main entry point -----------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        # Always advance underlying state
        opened = self._base.maybe_enter(bar, hist, equity, config)
        if opened is None:
            return None

        # No daily-verdict provider → behave identically to base
        if self._verdict_provider is None:
            return opened

        try:
            verdict = self._verdict_provider(bar.timestamp.date())
        except Exception:  # noqa: BLE001 - provider isolation
            return opened

        # Conviction below threshold → sage too uncertain, let trade fire
        if verdict.conviction < self.cfg.min_daily_conviction:
            return opened

        # Direction check
        if self.cfg.strict_mode:
            # Long requires sage bull, short requires sage bear; neutral vetoes
            if verdict.direction == "neutral":
                return None
            ok = (
                (opened.side == "BUY" and verdict.direction == "long")
                or (opened.side == "SELL" and verdict.direction == "short")
            )
        else:
            # Long requires sage NOT bear, short requires sage NOT bull
            ok = (
                (opened.side == "BUY" and verdict.direction != "short")
                or (opened.side == "SELL" and verdict.direction != "long")
            )

        if not ok:
            return None

        # Tag with sage daily verdict for audit
        new_tag = (
            f"{opened.regime}_sage_daily_{verdict.direction}_"
            f"conv{verdict.conviction:.2f}"
        )
        return replace(opened, regime=new_tag)
