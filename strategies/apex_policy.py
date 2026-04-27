"""EVOLUTIONARY TRADING ALGO  //  strategies.eta_policy.

The six AI-optimized Evolutionary Trading Algo strategies, composed from the pure
primitives in :mod:`eta_engine.strategies.smc_primitives`. Each
strategy is a pure function of (bars, context) -> StrategySignal.

Contract
--------
Every strategy returns a :class:`StrategySignal` with ``is_actionable``
True only when all of its required primitives fire AND the context
allows trading (regime, session, kill-switch etc. are supplied via the
``StrategyContext``).

The named strategies mirror the founder-brief ranking 1..6.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from eta_engine.strategies.models import (
    Bar,
    Side,
    StrategyId,
    StrategySignal,
)
from eta_engine.strategies.regime_exclusion import is_regime_excluded
from eta_engine.strategies.smc_primitives import (
    above_moving_average,
    detect_break_of_structure,
    detect_displacement,
    detect_fvg,
    detect_liquidity_sweep,
    detect_order_block,
)

# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy needs beyond raw bars.

    ``regime_label`` is the string form of the current
    :class:`eta_engine.brain.regime.RegimeType` -- kept as a string so
    this module doesn't import pydantic or the brain package.

    ``confluence_score`` is the 0..10 output of the existing
    :mod:`eta_engine.core.confluence_scorer`; strategies blend their
    own confidence against it rather than recomputing from scratch.

    ``vol_z`` is how many sigmas of volatility we are above baseline.
    Used to clamp risk in chop.
    """

    regime_label: str = "TRANSITION"
    confluence_score: float = 5.0
    vol_z: float = 0.0
    trend_bias: Side = Side.FLAT
    session_allows_entries: bool = True
    kill_switch_active: bool = False
    htf_bias: Side = Side.FLAT
    meta: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _blended_confidence(
    base: float,
    ctx: StrategyContext,
    *,
    floor: float = 0.0,
) -> float:
    """Blend the raw detector confidence with global confluence + vol.

    * Confluence boost: (confluence_score / 10.0) pulls the confidence
      up when the portfolio-level factors agree.
    * Vol penalty: ``max(0, vol_z - 1.5)`` clips the top end in very hot
      regimes so we don't scale up into chop.
    """
    boost = ctx.confluence_score / 10.0
    vol_penalty = max(0.0, ctx.vol_z - 1.5) * 0.5
    blended = (base * (0.5 + 0.5 * boost)) - vol_penalty
    return max(floor, min(10.0, blended))


def _risk_mult(ctx: StrategyContext, base_mult: float) -> float:
    """Scale risk down in high-vol / bad-regime / kill-switch states.

    Hard exclusions consult :mod:`eta_engine.strategies.regime_exclusion`
    so the live policy zeroes risk for any regime the latest OOS
    cross-regime validation has marked as untradeable. As of
    2026-04-17 this includes HIGH_VOL (sign-flip overfit) and CRISIS
    (unmodellable spreads). Edit
    ``docs/cross_regime/regime_exclusions.json`` to override.

    The legacy LOW_VOL hard-zero is preserved as a separate rule
    because it isn't an OOS finding -- it's a structural floor (no
    edge to mine when realised vol < 20% of baseline).
    """
    if ctx.kill_switch_active or not ctx.session_allows_entries:
        return 0.0
    decision = is_regime_excluded(ctx.regime_label)
    if decision.excluded:
        return 0.0
    mult = base_mult
    if ctx.regime_label == "LOW_VOL":
        mult *= 0.0
    if ctx.vol_z > 2.5:
        mult *= 0.5
    return max(0.0, min(mult, 1.5))


def _flat(strategy: StrategyId, tags: tuple[str, ...] = ()) -> StrategySignal:
    return StrategySignal(strategy=strategy, side=Side.FLAT, rationale_tags=tags)


# ---------------------------------------------------------------------------
# 1. Liquidity Sweep + Displacement Ambush
# ---------------------------------------------------------------------------


def liquidity_sweep_displacement(
    bars: list[Bar],
    ctx: StrategyContext,
) -> StrategySignal:
    """Highest-frequency predator hunt.

    Requires: equal-level sweep on last bar  AND  a displacement candle
    in the direction of the sweep-back. Confidence peaks when both fire
    on the same bar; falls if the displacement is stale.
    """
    strategy = StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT
    if len(bars) < 40 or not ctx.session_allows_entries:
        return _flat(strategy, ("insufficient_bars_or_session",))

    sweep = detect_liquidity_sweep(bars)
    disp = detect_displacement(bars)
    if sweep is None or disp is None:
        tags = (("no_sweep",) if sweep is None else ()) + (("no_displacement",) if disp is None else ())
        return _flat(strategy, tags)

    # Sweep-low + bull displacement = LONG; sweep-high + bear = SHORT
    if sweep.side.value == "LOW" and disp.direction is Side.LONG:
        side = Side.LONG
        entry = bars[-1].close
        stop = sweep.level - (sweep.level - bars[-1].low) * 1.1
        target = entry + (entry - stop) * 3.0
    elif sweep.side.value == "HIGH" and disp.direction is Side.SHORT:
        side = Side.SHORT
        entry = bars[-1].close
        stop = sweep.level + (bars[-1].high - sweep.level) * 1.1
        target = entry - (stop - entry) * 3.0
    else:
        return _flat(strategy, ("sweep_disp_direction_mismatch",))

    base_conf = min(10.0, 5.0 + sweep.depth_pct * 500.0 + (disp.body_mult - 1.8))
    conf = _blended_confidence(base_conf, ctx)
    return StrategySignal(
        strategy=strategy,
        side=side,
        entry=entry,
        stop=stop,
        target=target,
        confidence=conf,
        risk_mult=_risk_mult(ctx, 1.0),
        rationale_tags=(
            "liquidity_sweep",
            f"sweep_depth_pct={sweep.depth_pct:.4f}",
            f"displacement_mult={disp.body_mult:.2f}",
        ),
        meta={
            "sweep_level": sweep.level,
            "sweep_depth_pct": sweep.depth_pct,
            "displacement_body_mult": disp.body_mult,
        },
    )


# ---------------------------------------------------------------------------
# 2. Order Block / Breaker Retest with HTF Bias
# ---------------------------------------------------------------------------


def ob_breaker_retest(
    bars: list[Bar],
    ctx: StrategyContext,
) -> StrategySignal:
    """Core institutional ambush.

    Requires: (a) a BOS in the last ``window`` bars, (b) an order block
    upstream of the BOS, (c) price is currently retesting the OB zone,
    (d) HTF bias aligns with the BOS direction.
    """
    strategy = StrategyId.OB_BREAKER_RETEST
    if len(bars) < 10 or not ctx.session_allows_entries:
        return _flat(strategy, ("insufficient_bars_or_session",))

    bos = detect_break_of_structure(bars)
    if bos is None:
        return _flat(strategy, ("no_bos",))
    if ctx.htf_bias is not Side.FLAT and ctx.htf_bias is not bos.direction:
        return _flat(strategy, ("htf_bias_mismatch",))
    ob = detect_order_block(bars, bos)
    if ob is None:
        return _flat(strategy, ("no_ob",))

    last_close = bars[-1].close
    in_zone = ob.low <= last_close <= ob.high
    if not in_zone:
        return _flat(strategy, ("not_in_ob_zone",))

    if bos.direction is Side.LONG:
        entry = last_close
        stop = ob.low * 0.999
        target = entry + (entry - stop) * 2.5
    else:
        entry = last_close
        stop = ob.high * 1.001
        target = entry - (stop - entry) * 2.5

    base_conf = 6.5 + (1.0 if ctx.htf_bias is bos.direction else 0.0)
    conf = _blended_confidence(base_conf, ctx)
    return StrategySignal(
        strategy=strategy,
        side=bos.direction,
        entry=entry,
        stop=stop,
        target=target,
        confidence=conf,
        risk_mult=_risk_mult(ctx, 1.0),
        rationale_tags=(
            "break_of_structure",
            "order_block_retest",
            f"htf_bias={ctx.htf_bias.value}",
        ),
        meta={
            "ob_low": ob.low,
            "ob_high": ob.high,
            "bos_pivot": bos.pivot_price,
        },
    )


# ---------------------------------------------------------------------------
# 3. FVG Fill + Confluence Hunter
# ---------------------------------------------------------------------------


def fvg_fill_confluence(
    bars: list[Bar],
    ctx: StrategyContext,
) -> StrategySignal:
    """Imbalance sniper.

    Requires: an unfilled FVG in the direction of the current confluence
    bias, and price is inside the gap zone (i.e. about to fill).
    """
    strategy = StrategyId.FVG_FILL_CONFLUENCE
    if len(bars) < 5 or not ctx.session_allows_entries:
        return _flat(strategy, ("insufficient_bars_or_session",))

    fvg = detect_fvg(bars)
    if fvg is None:
        return _flat(strategy, ("no_fvg",))
    last_close = bars[-1].close
    in_zone = fvg.low <= last_close <= fvg.high
    if not in_zone:
        return _flat(strategy, ("price_not_in_fvg",))

    # Adaptive R:R -- wider in trending, tighter in ranging.
    rr = 3.0 if ctx.regime_label == "TRENDING" else 1.5
    if fvg.direction is Side.LONG:
        entry = last_close
        stop = fvg.low * 0.999
        target = entry + (entry - stop) * rr
    else:
        entry = last_close
        stop = fvg.high * 1.001
        target = entry - (stop - entry) * rr

    base_conf = 5.5
    if ctx.trend_bias is fvg.direction:
        base_conf += 1.5
    conf = _blended_confidence(base_conf, ctx)
    return StrategySignal(
        strategy=strategy,
        side=fvg.direction,
        entry=entry,
        stop=stop,
        target=target,
        confidence=conf,
        risk_mult=_risk_mult(ctx, 0.75),
        rationale_tags=(
            "fvg_fill",
            f"regime={ctx.regime_label}",
            f"rr={rr:.1f}",
        ),
        meta={
            "fvg_low": fvg.low,
            "fvg_high": fvg.high,
            "fvg_middle_bar": float(fvg.middle_bar_index),
        },
    )


# ---------------------------------------------------------------------------
# 4. Multi-Timeframe Trend-Following Ambush (200 MA + BOS)
# ---------------------------------------------------------------------------


def mtf_trend_following(
    bars: list[Bar],
    ctx: StrategyContext,
    *,
    ma_period: int = 200,
) -> StrategySignal:
    """Position-trading strategy.

    Only triggers when:
      * last close is on the correct side of the ``ma_period`` MA;
      * regime is TRENDING (chop cuts risk to zero);
      * a BOS confirms the direction.
    """
    strategy = StrategyId.MTF_TREND_FOLLOWING
    if len(bars) < ma_period + 5:
        return _flat(strategy, ("insufficient_bars",))

    trend = above_moving_average(bars, period=ma_period)
    if trend is Side.FLAT:
        return _flat(strategy, ("no_trend",))
    if ctx.regime_label != "TRENDING":
        return _flat(strategy, (f"regime_not_trending={ctx.regime_label}",))

    bos = detect_break_of_structure(bars)
    if bos is None or bos.direction is not trend:
        return _flat(strategy, ("bos_not_aligned_with_trend",))

    last_close = bars[-1].close
    ma = sum(bar.close for bar in bars[-ma_period:]) / float(ma_period)
    if trend is Side.LONG:
        entry = last_close
        stop = ma * 0.995
        target = entry + (entry - stop) * 2.0
    else:
        entry = last_close
        stop = ma * 1.005
        target = entry - (stop - entry) * 2.0

    base_conf = 7.0
    conf = _blended_confidence(base_conf, ctx)
    return StrategySignal(
        strategy=strategy,
        side=trend,
        entry=entry,
        stop=stop,
        target=target,
        confidence=conf,
        risk_mult=_risk_mult(ctx, 1.25),
        rationale_tags=(
            f"ma{ma_period}",
            "bos_aligned",
            "regime_trending",
        ),
        meta={
            "ma_period": float(ma_period),
            "ma_value": ma,
            "bos_pivot": bos.pivot_price,
        },
    )


# ---------------------------------------------------------------------------
# 5. Regime-Adaptive Portfolio Allocation (meta-strategy marker)
# ---------------------------------------------------------------------------


def regime_adaptive_allocation(
    bars: list[Bar],
    ctx: StrategyContext,
) -> StrategySignal:
    """Meta-strategy marker.

    The actual allocation math lives in
    :mod:`eta_engine.strategies.regime_allocator` (portfolio-level) and
    :mod:`eta_engine.funnel.waterfall` (layer-level). This function
    produces a *readout* signal so the decision journal can record that
    the allocator was consulted on this bar.
    """
    strategy = StrategyId.REGIME_ADAPTIVE_ALLOCATION
    if not bars:
        return _flat(strategy, ("no_bars",))
    conf = _blended_confidence(5.0, ctx)
    return StrategySignal(
        strategy=strategy,
        side=Side.FLAT,  # non-directional
        confidence=conf,
        risk_mult=_risk_mult(ctx, 0.0),  # sizing done upstream
        rationale_tags=(
            f"regime={ctx.regime_label}",
            f"vol_z={ctx.vol_z:.2f}",
        ),
        meta={
            "regime_label_ord": float(hash(ctx.regime_label) % 1000),
        },
    )


# ---------------------------------------------------------------------------
# 6. RL Full-Automation Policy marker
# ---------------------------------------------------------------------------


def rl_full_automation(
    bars: list[Bar],
    ctx: StrategyContext,
) -> StrategySignal:
    """RL marker.

    Actual policy inference lives in
    :mod:`eta_engine.strategies.rl_policy`. This function produces a
    no-op FLAT marker when the RL agent isn't available, so the router
    can still record attempts.
    """
    strategy = StrategyId.RL_FULL_AUTOMATION
    return _flat(strategy, ("rl_agent_external",))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


STRATEGIES: dict[StrategyId, object] = {
    StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: liquidity_sweep_displacement,
    StrategyId.OB_BREAKER_RETEST: ob_breaker_retest,
    StrategyId.FVG_FILL_CONFLUENCE: fvg_fill_confluence,
    StrategyId.MTF_TREND_FOLLOWING: mtf_trend_following,
    StrategyId.REGIME_ADAPTIVE_ALLOCATION: regime_adaptive_allocation,
    StrategyId.RL_FULL_AUTOMATION: rl_full_automation,
}
"""Id -> callable registry for the policy router + tests."""
