"""
EVOLUTIONARY TRADING ALGO  //  strategies.regime_gated_strategy
================================================================
Regime-conditional gate — wraps any sub-strategy with HTF regime
classification.

Why this exists
---------------
The 2026-04-27 5-year walk-forward (see
``docs/research_log/extended_data_walk_forward_20260427.md``) found
that the +6.00 OOS BTC champion was sample-specific: across 57
windows on 5 years of data, agg OOS dropped to +1.96 with 40%
positive folds. The strategy was NOT curve-fit (deg_avg stayed at
0.238 < 0.35) — it was regime-conditional. It works strongly in
some regimes (trending bull / low-vol consolidation) and is flat-
to-negative in others (volatile drawdown, choppy bear).

The path back toward the higher OOS is to detect WHICH regime the
strategy has edge in and gate firings to ONLY that regime. The 60%
of windows where the strategy was flat were dragging the agg OOS
down; if those firings are excluded by gate, the per-fire Sharpe
should stay near +6 territory and the all-fold average rises.

Design
------
This wrapper is GENERIC — works on any sub-strategy that exposes
``maybe_enter(bar, hist, equity, config) -> _Open | None``. The
caller supplies:
  1. The sub-strategy instance (e.g. ``SageDailyGatedStrategy`` or
     ``EnsembleVotingStrategy``).
  2. An ``HtfRegimeClassifier`` instance (caller chooses LTF vs HTF
     classification cadence + EMA periods).
  3. A ``RegimeGatedConfig`` listing which regimes/biases/modes
     should be allowed to fire (default: trending+ranging, any bias,
     trend_follow+mean_revert; only volatile is blocked).

On every bar:
  * The classifier is updated unconditionally so its EMAs evolve.
  * If the sub-strategy proposes an entry, we check the live
    classification. If allowed → forward; else → veto.

This is composition all the way down — no engine changes, no
sub-strategy changes. Strategies stack like Lego.

Interaction with side
---------------------
Optionally, ``require_bias_match_side`` makes longs only fire when
bias == 'long' and shorts only fire when bias == 'short'. This is
the strictest setting; default keeps both directions allowed when
the regime+mode are right.

Cross-asset use — read this before wrapping a non-BTC strategy
---------------------------------------------------------------
The wrapper is asset-agnostic by design but the **config knobs are
not portable across asset classes**. The HTF classifier's
EMA periods, slope threshold, trend-distance %, and ATR-% cutoffs
are all calibrated to a specific bar cadence + asset volatility.
Running BTC-daily knobs on MNQ-5m bars (or vice versa) silently
mis-classifies every bar.

Use the preset factories below — they bake in the right knobs per
asset class:

* ``btc_daily_preset()``  — BTC on 1h LTF with daily-cadence
  HTF classifier (EMA 50/200 ≈ 50d / 200d on 1h aggregated).
* ``mnq_intraday_preset()`` — MNQ on 5m LTF with 1h-cadence
  HTF classifier (EMA 20/60 ≈ 100m / 5h).

If you wrap an asset that doesn't have a preset yet, build one
explicitly rather than reusing a foreign-asset config. The presets
also bake in the right ``allowed_regimes`` / ``allowed_modes`` for
each asset's edge regime — see the per-preset docstring for the
rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from eta_engine.strategies.htf_regime_classifier import (
    HtfRegimeClassifier,
    HtfRegimeClassifierConfig,
)

if TYPE_CHECKING:
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
        ) -> _Open | None:
            ...


# Sensible defaults: drop volatile (where strategies bleed) but
# keep trending + ranging. trend_follow + mean_revert modes only.
_DEFAULT_ALLOWED_REGIMES: frozenset[str] = frozenset({"trending", "ranging"})
_DEFAULT_ALLOWED_BIASES: frozenset[str] = frozenset({"long", "short", "neutral"})
_DEFAULT_ALLOWED_MODES: frozenset[str] = frozenset({"trend_follow", "mean_revert"})


@dataclass(frozen=True)
class RegimeGatedConfig:
    """Knobs for the regime gate."""

    allowed_regimes: frozenset[str] = _DEFAULT_ALLOWED_REGIMES
    allowed_biases: frozenset[str] = _DEFAULT_ALLOWED_BIASES
    allowed_modes: frozenset[str] = _DEFAULT_ALLOWED_MODES

    # When True, BUY entries require bias == 'long', SELL entries
    # require bias == 'short'. Strictest setting; default OFF.
    require_bias_match_side: bool = False

    # Inner classifier config (passed through to HtfRegimeClassifier).
    classifier: HtfRegimeClassifierConfig = field(
        default_factory=HtfRegimeClassifierConfig,
    )


class RegimeGatedStrategy:
    """Wraps any sub-strategy with an HTF regime gate.

    The wrapper maintains its own ``HtfRegimeClassifier``. On every
    bar, the classifier is updated. When the sub-strategy proposes
    an entry, the live classification is consulted. Only allowed
    (regime, bias, mode) combinations pass through.

    Provider attachments are forwarded to the sub-strategy via
    ``__getattr__``, so callers can wire e.g. ETF flow / sage daily
    verdict providers as if the sub-strategy were the top-level
    object.
    """

    def __init__(
        self,
        sub_strategy: _SubStrategy,
        config: RegimeGatedConfig | None = None,
    ) -> None:
        self._sub = sub_strategy
        self.cfg = config or RegimeGatedConfig()
        self._classifier = HtfRegimeClassifier(self.cfg.classifier)
        # Audit counters — useful in walk-forward post-mortems
        self._n_seen: int = 0
        self._n_proposed: int = 0
        self._n_vetoed: int = 0
        self._n_allowed: int = 0

    # -- attribute forwarding -----------------------------------------------
    # Forward attach_* methods (and any unknown lookup) to the sub.
    # We DELIBERATELY do NOT __getattr__ everything because that would
    # break attribute checks elsewhere; instead we explicitly forward
    # the common provider-attachment names.

    def attach_daily_verdict_provider(self, provider: object) -> None:  # noqa: ANN001
        if hasattr(self._sub, "attach_daily_verdict_provider"):
            self._sub.attach_daily_verdict_provider(provider)  # type: ignore[attr-defined]

    def attach_etf_flow_provider(self, p: object) -> None:  # noqa: ANN001
        if hasattr(self._sub, "attach_etf_flow_provider"):
            self._sub.attach_etf_flow_provider(p)  # type: ignore[attr-defined]

    def attach_lth_provider(self, p: object) -> None:  # noqa: ANN001
        if hasattr(self._sub, "attach_lth_provider"):
            self._sub.attach_lth_provider(p)  # type: ignore[attr-defined]

    def attach_fear_greed_provider(self, p: object) -> None:  # noqa: ANN001
        if hasattr(self._sub, "attach_fear_greed_provider"):
            self._sub.attach_fear_greed_provider(p)  # type: ignore[attr-defined]

    def attach_macro_provider(self, p: object) -> None:  # noqa: ANN001
        if hasattr(self._sub, "attach_macro_provider"):
            self._sub.attach_macro_provider(p)  # type: ignore[attr-defined]

    def attach_eth_alignment_provider(self, p: object) -> None:  # noqa: ANN001
        if hasattr(self._sub, "attach_eth_alignment_provider"):
            self._sub.attach_eth_alignment_provider(p)  # type: ignore[attr-defined]

    # -- audit -------------------------------------------------------------

    @property
    def gate_stats(self) -> dict[str, int]:
        """Counters for walk-forward post-mortem visibility."""
        return {
            "bars_seen": self._n_seen,
            "entries_proposed": self._n_proposed,
            "entries_vetoed": self._n_vetoed,
            "entries_allowed": self._n_allowed,
        }

    # -- main entry point --------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        # Update the classifier unconditionally so its EMAs evolve
        self._n_seen += 1
        self._classifier.update(bar)

        # Always advance underlying state so its cooldowns/EMAs evolve
        opened = self._sub.maybe_enter(bar, hist, equity, config)
        if opened is None:
            return None

        self._n_proposed += 1

        # Read the live classification
        cls = self._classifier.classify(bar)

        # Gate logic
        regime_ok = cls.regime in self.cfg.allowed_regimes
        bias_ok = cls.bias in self.cfg.allowed_biases
        mode_ok = cls.mode in self.cfg.allowed_modes

        if self.cfg.require_bias_match_side:
            if opened.side == "BUY" and cls.bias != "long":
                self._n_vetoed += 1
                return None
            if opened.side == "SELL" and cls.bias != "short":
                self._n_vetoed += 1
                return None

        if not (regime_ok and bias_ok and mode_ok):
            self._n_vetoed += 1
            return None

        self._n_allowed += 1

        # Tag with regime classification for audit
        new_tag = (
            f"{opened.regime}_regime_{cls.regime}_{cls.bias}_{cls.mode}"
        )
        return replace(opened, regime=new_tag)


# ---------------------------------------------------------------------------
# Asset-class preset factories
# ---------------------------------------------------------------------------
# These bake in the calibration that's right for each asset class.
# Same wrapper code, but the knobs differ enough that mixing them
# silently mis-classifies. Always use a preset when you know the
# asset; only build a raw RegimeGatedConfig when prototyping a
# brand-new asset class.


def btc_daily_preset(*, strict_long_only: bool = False) -> RegimeGatedConfig:
    """Preset for BTC strategies whose LTF is 1h.

    The classifier runs on the LTF stream (1h bars) but its EMA
    periods are scaled so the slow EMA spans ~200 days (200 * 24h
    bars = 4800 bars). That gives the classifier a real "macro"
    read on BTC's regime — which matches the cadence at which the
    +6.00 champion actually had edge (multi-week trending bull
    consolidations).

    Edge regime for BTC: trending OR ranging, NOT volatile. The
    +6.00 champion was a directional long-bias swing strategy;
    when ``strict_long_only=True``, only LONG-bias bars allow BUYs.

    Trend-distance / ATR / slope thresholds are calibrated to BTC
    daily volatility (~3% daily ranges, ~1-2% ATR/close).
    """
    cls = HtfRegimeClassifierConfig(
        # 1h cadence — span 50 / 200 *days* of 1h bars.
        fast_ema=50 * 24,           # 1200 bars ~ 50 days
        slow_ema=200 * 24,          # 4800 bars ~ 200 days
        slope_lookback=24 * 5,      # 5-day slope window
        slope_threshold_pct=0.5,
        trend_distance_pct=3.0,
        range_atr_pct_max=2.0,
        atr_period=24 * 7,          # 7-day ATR
        warmup_bars=200 * 24 + 50,  # full slow-EMA fill
    )
    return RegimeGatedConfig(
        classifier=cls,
        allowed_regimes=frozenset({"trending", "ranging"}),
        allowed_biases=(
            frozenset({"long"}) if strict_long_only
            else frozenset({"long", "short", "neutral"})
        ),
        allowed_modes=frozenset({"trend_follow", "mean_revert"}),
        require_bias_match_side=strict_long_only,
    )


def mnq_intraday_preset() -> RegimeGatedConfig:
    """Preset for MNQ/NQ ORB-style strategies whose LTF is 5m.

    The classifier runs on the LTF stream (5m bars). EMA periods
    span ~5 hours / ~1 RTH-session so the classification reflects
    the intraday regime — which is what an ORB or breakout
    strategy cares about (range-vs-trend on the day's tape).

    Edge regime for ORB-style on index futures: ranging (range
    expansion is the mechanic). Trending tape often has the day's
    range exhausted before the breakout fires; volatile tape gives
    too many false breakouts. So ``allowed_regimes={ranging}`` and
    ``allowed_modes={mean_revert}`` is the calibrated default.

    Thresholds scaled to MNQ intraday volatility — index futures
    move ~0.5-1% per RTH session, ATR/close ~0.1-0.3%.
    """
    cls = HtfRegimeClassifierConfig(
        # 5m cadence — fast = 100 minutes, slow = 5 hours.
        fast_ema=20,                 # 100m
        slow_ema=60,                 # 300m = 5h
        slope_lookback=12,           # 1h slope window
        slope_threshold_pct=0.10,    # MNQ moves ~0.5% / RTH session
        trend_distance_pct=0.5,
        range_atr_pct_max=0.30,
        atr_period=12,               # 1h ATR
        warmup_bars=80,              # full slow-EMA fill
    )
    return RegimeGatedConfig(
        classifier=cls,
        allowed_regimes=frozenset({"ranging"}),
        allowed_biases=frozenset({"long", "short", "neutral"}),
        allowed_modes=frozenset({"mean_revert"}),
        require_bias_match_side=False,
    )


def eth_daily_preset() -> RegimeGatedConfig:
    """Preset for ETH on 1h LTF — same shape as BTC but slightly
    higher vol thresholds (ETH historically ~1.3x BTC vol).

    Use when running ETH variants of the +6.00 architecture once
    ETH ETF flow data is wired (Wave 1 fetcher already pulled 452
    days; provider attachment is the next step).
    """
    cls = HtfRegimeClassifierConfig(
        fast_ema=50 * 24,
        slow_ema=200 * 24,
        slope_lookback=24 * 5,
        slope_threshold_pct=0.5,
        trend_distance_pct=4.0,      # ETH moves more
        range_atr_pct_max=2.5,       # higher ATR cutoff
        atr_period=24 * 7,
        warmup_bars=200 * 24 + 50,
    )
    return RegimeGatedConfig(
        classifier=cls,
        allowed_regimes=frozenset({"trending", "ranging"}),
        allowed_biases=frozenset({"long", "short", "neutral"}),
        allowed_modes=frozenset({"trend_follow", "mean_revert"}),
        require_bias_match_side=False,
    )
