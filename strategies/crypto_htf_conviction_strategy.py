"""
EVOLUTIONARY TRADING ALGO  //  strategies.crypto_htf_conviction_strategy
========================================================================
HTF-conviction-sized regime trend.

User insight (2026-04-27): "wouldn't these [Tier-4 signals] make
more sense at higher time frames... bigger confluence trades with
a little higher risk?"

Architecture
------------
HTF REGIME LAYER (HtfRegimeOracle)
  reads ETF + LTH + F&G + macro + HTF-EMA on natural cadence
  -> (direction, conviction in [0, 1])

EXECUTION LAYER (this strategy)
  delegates 1h entries to crypto_regime_trend
  gates direction on the oracle's verdict
  scales position size by conviction

The conviction-to-size mapping is configurable (step function or
linear) and bounded so a high-conviction trade can take more risk
than baseline (the user's "a little higher risk") but never blow
through a sane cap.

Honest scope
------------
* This is NOT a black box: every decision is auditable via the
  HtfRegimeReport's per-component dict + the strategy's regime tag.
* When the oracle's direction is "neutral" (composite below
  threshold), NO trade fires regardless of what the base strategy
  says. The HTF layer has veto power.
* Position scaling caps at ``max_size_multiplier`` so a euphoric
  conviction reading can't 3x the risk envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from eta_engine.strategies.crypto_regime_trend_strategy import (
    CryptoRegimeTrendConfig,
    CryptoRegimeTrendStrategy,
)
from eta_engine.strategies.htf_regime_oracle import (
    HtfRegimeOracle,
    HtfRegimeOracleConfig,
)

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData
    from eta_engine.strategies.htf_regime_oracle import HtfRegimeReport


@dataclass(frozen=True)
class HtfConvictionSizingConfig:
    """How conviction maps to position-size multiplier."""

    # Below this conviction the trade is skipped entirely (no fire).
    min_conviction_to_trade: float = 0.30

    # Linear sizing: multiplier = base + (conviction - 0.5) * gain.
    # base=1.0, gain=1.0 -> at conv=0.3 mult=0.8, conv=0.5 mult=1.0,
    # conv=0.8 mult=1.3, conv=1.0 mult=1.5.
    base_multiplier: float = 1.0
    conviction_gain: float = 1.0

    # Hard caps so position can't grow / shrink absurdly.
    # Capped at 1.3x — was 2.0, which on a funded account with daily DD
    # limits doubles tail-amplification risk on a "high conviction" bar
    # that may itself be high-correlation noise from the sage schools.
    min_size_multiplier: float = 0.5
    max_size_multiplier: float = 1.3


@dataclass(frozen=True)
class CryptoHtfConvictionConfig:
    """Combined base + oracle + sizing config."""

    base: CryptoRegimeTrendConfig = field(default_factory=CryptoRegimeTrendConfig)
    oracle: HtfRegimeOracleConfig = field(default_factory=HtfRegimeOracleConfig)
    sizing: HtfConvictionSizingConfig = field(
        default_factory=HtfConvictionSizingConfig,
    )


def _conviction_multiplier(
    cfg: HtfConvictionSizingConfig, conviction: float,
) -> float:
    """Map conviction in [0, 1] to a size multiplier (linear ramp)."""
    raw = cfg.base_multiplier + (conviction - 0.5) * cfg.conviction_gain
    return max(cfg.min_size_multiplier, min(cfg.max_size_multiplier, raw))


class CryptoHtfConvictionStrategy:
    """HTF-direction-gated, conviction-sized regime trend.

    Composition pattern: embeds CryptoRegimeTrendStrategy + an
    HtfRegimeOracle. On each bar, the oracle's HTF EMA is updated
    first; when the base strategy proposes an entry, the oracle's
    direction must agree (long/short — neutral always vetoes) and
    the entry's qty + risk_usd are scaled by the conviction
    multiplier before returning.
    """

    def __init__(
        self, config: CryptoHtfConvictionConfig | None = None,
    ) -> None:
        self.cfg = config or CryptoHtfConvictionConfig()
        self._base = CryptoRegimeTrendStrategy(self.cfg.base)
        self._oracle = HtfRegimeOracle(self.cfg.oracle)
        self._last_report: HtfRegimeReport | None = None

    # -- provider attachment (proxied to the oracle) -------------------------

    def attach_etf_flow_provider(self, provider: object) -> None:  # noqa: ANN001
        self._oracle._etf = provider  # type: ignore[assignment]

    def attach_lth_provider(self, provider: object) -> None:  # noqa: ANN001
        self._oracle._lth = provider  # type: ignore[assignment]

    def attach_fear_greed_provider(self, provider: object) -> None:  # noqa: ANN001
        self._oracle._fg = provider  # type: ignore[assignment]

    def attach_macro_provider(self, provider: object) -> None:  # noqa: ANN001
        self._oracle._macro = provider  # type: ignore[assignment]

    @property
    def last_report(self) -> HtfRegimeReport | None:
        """The most recent oracle report. Useful for tests + audit."""
        return self._last_report

    # -- main entry point -----------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        # 1. Update the oracle's HTF EMA on every bar (even pre-warmup)
        self._oracle.update_htf_ema(bar.close)

        # 2. Get the oracle's regime read for THIS bar
        report = self._oracle.regime_for(bar)
        self._last_report = report

        # 3. ALWAYS delegate to base so its state (EMAs, cooldowns,
        # day counters) advances every bar. We discard the proposal
        # later if the oracle vetoes — but skipping the call here
        # would freeze the base's internal EMAs whenever the oracle
        # is neutral (which is most of the time during warmup).
        opened = self._base.maybe_enter(bar, hist, equity, config)

        # 4. Hard veto if direction is neutral (regardless of base).
        # If base produced a candidate, roll back its cooldown so
        # the next aligned bar can fire.
        if report.direction == "neutral":
            if opened is not None:
                self._rollback_base()
            return None

        # 5. Hard veto if conviction below threshold
        if report.conviction < self.cfg.sizing.min_conviction_to_trade:
            if opened is not None:
                self._rollback_base()
            return None

        if opened is None:
            return None

        # 6. Direction alignment — if base side disagrees with oracle, skip
        oracle_side = "BUY" if report.direction == "long" else "SELL"
        if opened.side != oracle_side:
            self._rollback_base()
            return None

        # 7. Conviction-scaled size
        mult = _conviction_multiplier(self.cfg.sizing, report.conviction)
        scaled_qty = opened.qty * mult
        scaled_risk_usd = opened.risk_usd * mult

        # 8. Annotate regime tag with conviction band so the audit
        # trail shows which "tier" the trade fired in.
        if mult >= 1.4:
            band = "high"
        elif mult >= 1.0:
            band = "mid"
        else:
            band = "low"
        regime_tag = f"htf_conv_{report.direction}_{band}"

        return replace(
            opened,
            qty=scaled_qty,
            risk_usd=scaled_risk_usd,
            regime=regime_tag,
        )

    # -- helpers --------------------------------------------------------------

    def _rollback_base(self) -> None:
        """Clear base strategy's cooldown so a later HTF-aligned bar
        can still fire (the entry didn't actually happen)."""
        self._base._trades_today = max(0, self._base._trades_today - 1)
        if self._base._last_entry_idx is not None:
            self._base._last_entry_idx = (
                self._base._bars_seen - self.cfg.base.min_bars_between_trades - 1
            )
