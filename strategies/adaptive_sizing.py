"""EVOLUTIONARY TRADING ALGO  //  strategies.adaptive_sizing.

Regime-aware, self-evolving position sizer.

The sniper-shot principle
-------------------------
The founder's mental model (verbatim):

  "Anything rated super high confidence of success needs to be
  sized 2-3 times bigger than normal. Anything rated low chance or
  too much uncertainty not enough confluence gets sized down. Risk
  is protective but profit is the goal in the long run so sometimes
  you have to roll the dice when the odds are stacked in your favor
  and you take out the sniper and shoot from over 2 miles away
  because that's what you planned to do."

This module is the mathematical expression of that brief. It is a
pure-function sizer -- no I/O, no threads, no async -- that takes a
:class:`SizingContext` (asset, strategy, regime, confluence, equity
band, prior-success metrics) and emits a :class:`SizingVerdict`
containing:

  * ``tier`` -- categorical tag: CONVICTION | STANDARD | REDUCED |
    PROBE | SKIP.
  * ``multiplier`` -- continuous scalar the risk layer multiplies
    the per-trade risk % by.
  * ``adjusted_risk_pct`` -- the clamped product.
  * ``confidence_score`` -- 0..1 overall "should I take this" score
    useful for dashboards and ranking.
  * ``rationale`` -- a human-readable tuple explaining which axes
    contributed, so the founder can audit the sizer's decisions.
  * ``axis_scores`` -- per-axis breakdown for the retrospective
    engine (v0.1.47).

Axes scored
-----------
1. Regime alignment -- TRENDING directional setups get a boost;
   RANGING mean-reversion setups get a boost; mismatched pairings
   get 0; HIGH_VOL gets a negative score to reflect the v0.1.37
   exclusion gate's spirit.
2. Confluence score -- linear in (score - 5.0) / 5.0, clamped
   [-1, +1].
3. HTF bias agreement -- does the higher-timeframe bias agree
   with the signal direction? +0.5 / -0.5 / 0 for flat bias.
4. Equity band -- GROWTH lean-in, NEUTRAL business-as-usual,
   DRAWDOWN tighten, CRITICAL probe-only.
5. Prior success -- rolling expectancy on this
   (strategy, asset, regime) bucket. Strong positive expectancy
   amplifies; losing streaks suppress.
6. Kill switch / session gate -- hard overrides. If either is
   active, the verdict is SKIP regardless of the other axes.

Tier thresholds
---------------
The axis scores are summed with configurable weights. The total
score maps onto tiers by default:

    total >= +3.0   -> CONVICTION, multiplier = 3.0 (clamped)
    total >= +2.0   -> CONVICTION, multiplier = 2.0
    total >= +0.5   -> STANDARD,   multiplier = 1.0
    total >= -0.5   -> REDUCED,    multiplier = 0.5
    total >= -2.0   -> PROBE,      multiplier = 0.25
    total <  -2.0   -> SKIP,       multiplier = 0.0

These thresholds and weights are all exposed on
:class:`SizingPolicy` so the operator can retune without editing
the sizing math.

Self-evolving
-------------
:class:`PriorSuccessMetrics` is designed to be fed by the live
trade history: after every closed trade the risk layer updates
the rolling window for that (strategy, asset, regime) bucket.
Over time the sizer learns which strategy + regime combinations
are actually paying out and sizes them larger; which are bleeding
and sizes them smaller. The feedback loop is outside this module
(the retrospective engine owns it) but :class:`PriorSuccessMetrics`
is the plug point.

Safety bounds
-------------
:class:`SizingPolicy` exposes ``min_risk_pct`` and ``max_risk_pct``.
The final ``adjusted_risk_pct`` is clamped to [min, max] so even
a runaway CONVICTION bet can't exceed a hard ceiling (default:
5% per trade). The floor ensures PROBE sizing is still a real
position, not rounding error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.strategies.models import Side, StrategyId

__all__ = [
    "DEFAULT_SIZING_POLICY",
    "EquityBand",
    "PriorSuccessMetrics",
    "RegimeLabel",
    "SizeTier",
    "SizingContext",
    "SizingPolicy",
    "SizingVerdict",
    "classify_equity_band",
    "compute_size",
    "score_confluence",
    "score_equity_band",
    "score_htf_agreement",
    "score_prior_success",
    "score_regime",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RegimeLabel(StrEnum):
    """Wire-compatible with :mod:`strategies.eta_policy` StrategyContext.regime_label."""

    TRENDING = "TREND"
    RANGING = "RANGE"
    TRANSITION = "TRANSITION"
    HIGH_VOL = "HIGH_VOL"


class EquityBand(StrEnum):
    """Coarse banding of current equity vs high-water.

    Rationale: different equity bands demand different aggression
    profiles (founder brief):

      * GROWTH   -- equity at or above recent high; room to press.
      * NEUTRAL  -- equity near recent high; business as usual.
      * DRAWDOWN -- meaningful pullback; tighten sizing and take
        only higher-conviction setups.
      * CRITICAL -- serious drawdown; probe-sized only, and the
        retrospective engine should be interrogating every trade.
    """

    GROWTH = "GROWTH"
    NEUTRAL = "NEUTRAL"
    DRAWDOWN = "DRAWDOWN"
    CRITICAL = "CRITICAL"


class SizeTier(StrEnum):
    """Tiered size buckets the sizer emits.

    CONVICTION is subdivided on the continuous multiplier axis
    (2.0 vs 3.0) to keep the categorical label stable when the
    policy nudges the upper threshold."""

    CONVICTION = "CONVICTION"
    STANDARD = "STANDARD"
    REDUCED = "REDUCED"
    PROBE = "PROBE"
    SKIP = "SKIP"


# ---------------------------------------------------------------------------
# Input dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriorSuccessMetrics:
    """Rolling-window performance metrics for one
    (strategy, asset, regime) bucket.

    All values are computed from the last ``n_trades`` closed trades
    in that bucket. The sizer consults them to decide whether this
    combination is currently in form.

    A fresh / untested bucket is represented by ``n_trades == 0``;
    the sizer treats this as "no opinion" (zero contribution to
    the axis score) -- neither boosting nor penalizing.
    """

    n_trades: int = 0
    hit_rate: float = 0.0  # 0..1
    expectancy_r: float = 0.0  # avg R per trade (1R = per-trade risk)
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0  # negative
    consecutive_losses: int = 0
    consecutive_wins: int = 0

    @property
    def is_empty(self) -> bool:
        return self.n_trades == 0


@dataclass(frozen=True, slots=True)
class SizingContext:
    """Inputs to :func:`compute_size`.

    All fields are read-only. Build a new context per trade
    decision; do not mutate.
    """

    asset: str
    strategy: StrategyId
    side: Side  # from :class:`strategies.models.Side`
    regime: RegimeLabel
    confluence_score: float  # 0..10 (bot core_confluence)
    htf_bias: Side | None  # from context_from_dict
    equity_band: EquityBand
    prior: PriorSuccessMetrics = field(default_factory=PriorSuccessMetrics)
    base_risk_pct: float = 1.0  # BotConfig.risk_per_trade_pct
    kill_switch_active: bool = False
    session_allows_entries: bool = True


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SizingPolicy:
    """Operator-tunable thresholds, weights, and safety bounds.

    The default policy lines up with the founder brief
    (CONVICTION at 2-3x, PROBE at 0.25x, safety max at 5%).
    Retune without touching :func:`compute_size` itself.
    """

    # Axis weights. A higher weight makes that axis dominate the
    # total score. They sum to ~1.0 with the defaults but the math
    # does not require that -- they are pure multipliers.
    weight_regime: float = 0.25
    weight_confluence: float = 0.30
    weight_htf: float = 0.10
    weight_equity: float = 0.15
    weight_prior: float = 0.20

    # Tier thresholds on the weighted total. Must be strictly
    # decreasing. Tuned against the maximum-positive ceiling of
    # ~0.875 (every axis saturated) so CONVICTION-3x is reserved
    # for genuine sniper-shot setups, not merely good ones.
    conviction_high_threshold: float = 0.65  # 3.0x
    conviction_low_threshold: float = 0.45  # 2.0x
    standard_threshold: float = 0.15  # 1.0x
    reduced_threshold: float = -0.10  # 0.5x
    probe_threshold: float = -0.35  # 0.25x
    # total < probe_threshold -> SKIP

    # Multipliers per tier. CONVICTION has two variants picked on
    # total score; the others are fixed.
    conviction_high_mult: float = 3.0
    conviction_low_mult: float = 2.0
    standard_mult: float = 1.0
    reduced_mult: float = 0.5
    probe_mult: float = 0.25
    skip_mult: float = 0.0

    # Safety bounds on the final adjusted_risk_pct.
    min_risk_pct: float = 0.10
    max_risk_pct: float = 5.00


DEFAULT_SIZING_POLICY: SizingPolicy = SizingPolicy()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SizingVerdict:
    """The sizer's decision for one trade.

    * ``tier``             -- categorical bucket for dashboards /
      logging.
    * ``multiplier``       -- continuous scalar; risk layer does
      ``adjusted_risk_pct = base_risk_pct * multiplier``.
    * ``base_risk_pct``    -- from the input context (not
      recomputed here).
    * ``adjusted_risk_pct``-- clamped to ``[min_risk_pct,
      max_risk_pct]``. PROBE and SKIP are allowed to fall below
      the floor since their intent is "small" or "none".
    * ``confidence_score`` -- 0..1 "probability this trade is
      worth taking" view, derived from the same axis scores.
    * ``rationale``        -- per-axis human-readable notes.
    * ``axis_scores``      -- raw per-axis values (before
      weighting) so downstream consumers can recompute or
      diff against a different policy.
    """

    tier: SizeTier
    multiplier: float
    base_risk_pct: float
    adjusted_risk_pct: float
    confidence_score: float
    rationale: tuple[str, ...]
    axis_scores: dict[str, float]


# ---------------------------------------------------------------------------
# Axis scorers (pure functions)
# ---------------------------------------------------------------------------


def score_regime(
    regime: RegimeLabel,
    strategy: StrategyId,
) -> float:
    """Return -1..+1 based on regime / strategy fit.

    Directional strategies reward TRENDING; mean-reversion
    strategies reward RANGING; TRANSITION is neutral; HIGH_VOL is
    penalized across the board (matches v0.1.37's exclusion spirit).
    """
    from eta_engine.strategies.models import StrategyId as _StrategyIdCls

    if regime is RegimeLabel.HIGH_VOL:
        return -1.0
    if regime is RegimeLabel.TRANSITION:
        return 0.0

    directional = {
        _StrategyIdCls.MTF_TREND_FOLLOWING,
        _StrategyIdCls.LIQUIDITY_SWEEP_DISPLACEMENT,
        _StrategyIdCls.OB_BREAKER_RETEST,
        _StrategyIdCls.REGIME_ADAPTIVE_ALLOCATION,
        _StrategyIdCls.RL_FULL_AUTOMATION,
    }
    mean_rev = {_StrategyIdCls.FVG_FILL_CONFLUENCE}

    if regime is RegimeLabel.TRENDING:
        if strategy in directional:
            return 1.0
        if strategy in mean_rev:
            return -0.5
        return 0.0
    # RANGING
    if strategy in mean_rev:
        return 1.0
    if strategy in directional:
        return -0.3
    return 0.0


def score_confluence(score: float) -> float:
    """Map 0..10 confluence onto -1..+1.

    Linear in (score - 5.0) / 5.0, clamped. Matches the founder's
    spec: 5 is neutral, 10 is conviction, 0 is rejection."""
    return max(-1.0, min(1.0, (score - 5.0) / 5.0))


def score_htf_agreement(
    htf_bias: Side | None,
    side: Side,
) -> float:
    """Return +0.5 on agreement, -0.5 on disagreement, 0 on flat."""
    from eta_engine.strategies.models import Side as _SideCls

    if htf_bias is None or htf_bias is _SideCls.FLAT:
        return 0.0
    if side is _SideCls.FLAT:
        return 0.0
    return 0.5 if htf_bias is side else -0.5


def score_equity_band(band: EquityBand) -> float:
    """Map equity band to a scalar lean-in / lean-out."""
    return {
        EquityBand.GROWTH: 0.5,
        EquityBand.NEUTRAL: 0.0,
        EquityBand.DRAWDOWN: -0.5,
        EquityBand.CRITICAL: -1.0,
    }[band]


def score_prior_success(prior: PriorSuccessMetrics) -> float:
    """Map rolling bucket performance to -1..+1.

    Logic:
      * no data          -> 0.0 (no opinion)
      * expectancy > 0.5R -> strong boost
      * expectancy < -0.3R -> strong penalty
      * 3+ consecutive losses -> additional penalty
      * 3+ consecutive wins   -> additional boost

    Final score is clamped to [-1, +1].
    """
    if prior.is_empty:
        return 0.0

    # Primary contribution: expectancy in R-units.
    # +0.5R -> full boost; -0.5R -> full penalty.
    s = max(-1.0, min(1.0, prior.expectancy_r / 0.5))

    # Streak adjustments (additive, small).
    if prior.consecutive_losses >= 3:
        s -= 0.2
    if prior.consecutive_wins >= 3:
        s += 0.2

    return max(-1.0, min(1.0, s))


# ---------------------------------------------------------------------------
# Equity-band classifier
# ---------------------------------------------------------------------------


def classify_equity_band(
    equity: float,
    high_water: float,
    *,
    growth_threshold: float = 1.02,
    drawdown_threshold: float = 0.95,
    critical_threshold: float = 0.90,
) -> EquityBand:
    """Classify current equity against a high-water mark.

    Parameters
    ----------
    equity:
        Current account equity.
    high_water:
        Highest equity observed in the current tracking window.
        Must be strictly positive -- callers are expected to seed
        it with the account starting capital on the first tick.
    growth_threshold:
        equity / high_water >= this -> :attr:`EquityBand.GROWTH`.
    drawdown_threshold:
        Below this ratio -> :attr:`EquityBand.DRAWDOWN` (or worse).
    critical_threshold:
        Below this ratio -> :attr:`EquityBand.CRITICAL`.
    """
    if high_water <= 0.0:
        msg = f"high_water must be > 0, got {high_water!r}"
        raise ValueError(msg)
    ratio = equity / high_water
    if ratio >= growth_threshold:
        return EquityBand.GROWTH
    if ratio < critical_threshold:
        return EquityBand.CRITICAL
    if ratio < drawdown_threshold:
        return EquityBand.DRAWDOWN
    return EquityBand.NEUTRAL


# ---------------------------------------------------------------------------
# Core sizer
# ---------------------------------------------------------------------------


def compute_size(
    ctx: SizingContext,
    policy: SizingPolicy = DEFAULT_SIZING_POLICY,
) -> SizingVerdict:
    """Pure-function sizer. Returns a :class:`SizingVerdict`.

    Hard overrides (kill-switch, session gate) short-circuit to
    SKIP with a clear rationale. Otherwise the six axes are
    scored, weighted, summed, and mapped to a tier.
    """
    # Hard gates first -- kill-switch is non-negotiable.
    if ctx.kill_switch_active:
        return _skip(
            ctx,
            policy,
            reason="kill_switch_active",
        )
    if not ctx.session_allows_entries:
        return _skip(
            ctx,
            policy,
            reason="session_disallows_entries",
        )

    # Per-axis raw scores.
    s_regime = score_regime(ctx.regime, ctx.strategy)
    s_confluence = score_confluence(ctx.confluence_score)
    s_htf = score_htf_agreement(ctx.htf_bias, ctx.side)
    s_equity = score_equity_band(ctx.equity_band)
    s_prior = score_prior_success(ctx.prior)

    # Weighted total.
    total = (
        policy.weight_regime * s_regime
        + policy.weight_confluence * s_confluence
        + policy.weight_htf * s_htf
        + policy.weight_equity * s_equity
        + policy.weight_prior * s_prior
    )

    tier, multiplier = _tier_for_total(total, policy)

    adjusted = ctx.base_risk_pct * multiplier
    if tier not in (SizeTier.PROBE, SizeTier.SKIP):
        # Clamp CONVICTION / STANDARD / REDUCED into [min, max].
        adjusted = max(policy.min_risk_pct, min(policy.max_risk_pct, adjusted))
    elif tier is SizeTier.PROBE:
        # Probes are intentionally small but still real positions.
        # Keep above zero but don't clamp to min_risk_pct.
        adjusted = max(0.01, min(policy.max_risk_pct, adjusted))
    # SKIP stays at 0.

    # Confidence score is a bounded mapping of total into [0, 1].
    confidence = _confidence_from_total(total)

    rationale = _build_rationale(
        ctx,
        tier,
        s_regime,
        s_confluence,
        s_htf,
        s_equity,
        s_prior,
        total,
    )

    return SizingVerdict(
        tier=tier,
        multiplier=multiplier,
        base_risk_pct=ctx.base_risk_pct,
        adjusted_risk_pct=adjusted,
        confidence_score=confidence,
        rationale=rationale,
        axis_scores={
            "regime": s_regime,
            "confluence": s_confluence,
            "htf": s_htf,
            "equity": s_equity,
            "prior": s_prior,
            "total_weighted": total,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tier_for_total(
    total: float,
    policy: SizingPolicy,
) -> tuple[SizeTier, float]:
    if total >= policy.conviction_high_threshold:
        return SizeTier.CONVICTION, policy.conviction_high_mult
    if total >= policy.conviction_low_threshold:
        return SizeTier.CONVICTION, policy.conviction_low_mult
    if total >= policy.standard_threshold:
        return SizeTier.STANDARD, policy.standard_mult
    if total >= policy.reduced_threshold:
        return SizeTier.REDUCED, policy.reduced_mult
    if total >= policy.probe_threshold:
        return SizeTier.PROBE, policy.probe_mult
    return SizeTier.SKIP, policy.skip_mult


def _confidence_from_total(total: float) -> float:
    """Map weighted total (~[-1, +1]) onto [0, 1]."""
    # Clamp before mapping; total can nominally exceed +/-1 if all
    # axes saturate and weights sum > 1.
    t = max(-1.0, min(1.0, total))
    return (t + 1.0) / 2.0


def _skip(
    ctx: SizingContext,
    policy: SizingPolicy,
    *,
    reason: str,
) -> SizingVerdict:
    return SizingVerdict(
        tier=SizeTier.SKIP,
        multiplier=policy.skip_mult,
        base_risk_pct=ctx.base_risk_pct,
        adjusted_risk_pct=0.0,
        confidence_score=0.0,
        rationale=(f"hard_override:{reason}",),
        axis_scores={
            "regime": 0.0,
            "confluence": 0.0,
            "htf": 0.0,
            "equity": 0.0,
            "prior": 0.0,
            "total_weighted": 0.0,
        },
    )


def _build_rationale(
    ctx: SizingContext,
    tier: SizeTier,
    s_regime: float,
    s_confluence: float,
    s_htf: float,
    s_equity: float,
    s_prior: float,
    total: float,
) -> tuple[str, ...]:
    parts: list[str] = [
        f"tier={tier.value}",
        f"total={total:+.3f}",
        f"regime={ctx.regime.value}:{s_regime:+.2f}",
        f"confluence={ctx.confluence_score:.1f}:{s_confluence:+.2f}",
        f"htf={s_htf:+.2f}",
        f"equity={ctx.equity_band.value}:{s_equity:+.2f}",
    ]
    if not ctx.prior.is_empty:
        parts.append(
            f"prior(n={ctx.prior.n_trades},exp={ctx.prior.expectancy_r:+.2f}R):{s_prior:+.2f}",
        )
    else:
        parts.append("prior=none")
    return tuple(parts)
