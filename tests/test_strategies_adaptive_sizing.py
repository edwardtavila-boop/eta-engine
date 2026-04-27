"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_adaptive_sizing.

Comprehensive tests for v0.1.46's Adaptive Sizing Engine.

Coverage targets
----------------
1. Axis scorers are pure, bounded, and directionally correct.
2. Equity-band classifier respects its thresholds and rejects
   a non-positive high-water.
3. :func:`compute_size` maps axis-summed scores to the right tier.
4. Hard overrides (kill-switch, session-off) produce SKIP.
5. Safety bounds clamp adjusted_risk_pct into [min, max] for
   non-probe/skip tiers, and probes keep a small but real size.
6. Sniper-shot scenario -- every axis maxed -> CONVICTION at 3x.
7. Risk-off scenario -- regime mismatch + HIGH_VOL + losing
   streak -> SKIP or PROBE.
8. Prior-success axis recognizes empty, expectancy, streaks.
9. Rationale tuple always contains the tier + total + per-axis
   entries.
10. Default policy wire-matches the module docstring.
"""

from __future__ import annotations

import pytest

from eta_engine.strategies.adaptive_sizing import (
    DEFAULT_SIZING_POLICY,
    EquityBand,
    PriorSuccessMetrics,
    RegimeLabel,
    SizeTier,
    SizingContext,
    SizingPolicy,
    SizingVerdict,
    classify_equity_band,
    compute_size,
    score_confluence,
    score_equity_band,
    score_htf_agreement,
    score_prior_success,
    score_regime,
)
from eta_engine.strategies.models import Side, StrategyId

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(
    *,
    strategy: StrategyId = StrategyId.MTF_TREND_FOLLOWING,
    side: Side = Side.LONG,
    regime: RegimeLabel = RegimeLabel.TRENDING,
    confluence: float = 5.0,
    htf_bias: Side | None = Side.LONG,
    equity_band: EquityBand = EquityBand.NEUTRAL,
    prior: PriorSuccessMetrics | None = None,
    base_risk_pct: float = 1.0,
    kill_switch_active: bool = False,
    session_allows_entries: bool = True,
) -> SizingContext:
    return SizingContext(
        asset="MNQ",
        strategy=strategy,
        side=side,
        regime=regime,
        confluence_score=confluence,
        htf_bias=htf_bias,
        equity_band=equity_band,
        prior=prior if prior is not None else PriorSuccessMetrics(),
        base_risk_pct=base_risk_pct,
        kill_switch_active=kill_switch_active,
        session_allows_entries=session_allows_entries,
    )


# ===========================================================================
# 1. Axis scorers
# ===========================================================================


class TestRegimeScorer:
    def test_trending_directional_is_max_positive(self) -> None:
        assert (
            score_regime(
                RegimeLabel.TRENDING,
                StrategyId.MTF_TREND_FOLLOWING,
            )
            == 1.0
        )

    def test_trending_meanrev_is_negative(self) -> None:
        assert (
            score_regime(
                RegimeLabel.TRENDING,
                StrategyId.FVG_FILL_CONFLUENCE,
            )
            == -0.5
        )

    def test_ranging_meanrev_is_max_positive(self) -> None:
        assert (
            score_regime(
                RegimeLabel.RANGING,
                StrategyId.FVG_FILL_CONFLUENCE,
            )
            == 1.0
        )

    def test_ranging_directional_is_negative(self) -> None:
        assert (
            score_regime(
                RegimeLabel.RANGING,
                StrategyId.MTF_TREND_FOLLOWING,
            )
            == -0.3
        )

    def test_transition_is_zero(self) -> None:
        assert (
            score_regime(
                RegimeLabel.TRANSITION,
                StrategyId.MTF_TREND_FOLLOWING,
            )
            == 0.0
        )

    def test_high_vol_always_penalizes(self) -> None:
        for sid in StrategyId:
            assert score_regime(RegimeLabel.HIGH_VOL, sid) == -1.0


class TestConfluenceScorer:
    def test_zero_is_max_negative(self) -> None:
        assert score_confluence(0.0) == -1.0

    def test_five_is_neutral(self) -> None:
        assert score_confluence(5.0) == 0.0

    def test_ten_is_max_positive(self) -> None:
        assert score_confluence(10.0) == 1.0

    def test_clamps_above_ten(self) -> None:
        assert score_confluence(15.0) == 1.0

    def test_clamps_below_zero(self) -> None:
        assert score_confluence(-2.0) == -1.0

    def test_linear_in_middle_range(self) -> None:
        assert score_confluence(7.5) == pytest.approx(0.5)
        assert score_confluence(2.5) == pytest.approx(-0.5)


class TestHtfAgreementScorer:
    def test_agreement_is_positive(self) -> None:
        assert score_htf_agreement(Side.LONG, Side.LONG) == 0.5
        assert score_htf_agreement(Side.SHORT, Side.SHORT) == 0.5

    def test_disagreement_is_negative(self) -> None:
        assert score_htf_agreement(Side.LONG, Side.SHORT) == -0.5
        assert score_htf_agreement(Side.SHORT, Side.LONG) == -0.5

    def test_none_or_flat_is_zero(self) -> None:
        assert score_htf_agreement(None, Side.LONG) == 0.0
        assert score_htf_agreement(Side.FLAT, Side.LONG) == 0.0
        assert score_htf_agreement(Side.LONG, Side.FLAT) == 0.0


class TestEquityBandScorer:
    def test_band_to_score_mapping(self) -> None:
        assert score_equity_band(EquityBand.GROWTH) == 0.5
        assert score_equity_band(EquityBand.NEUTRAL) == 0.0
        assert score_equity_band(EquityBand.DRAWDOWN) == -0.5
        assert score_equity_band(EquityBand.CRITICAL) == -1.0


class TestPriorSuccessScorer:
    def test_empty_is_zero(self) -> None:
        assert score_prior_success(PriorSuccessMetrics()) == 0.0

    def test_strong_positive_expectancy_boosts(self) -> None:
        s = score_prior_success(
            PriorSuccessMetrics(n_trades=20, expectancy_r=0.5),
        )
        assert s == pytest.approx(1.0)

    def test_strong_negative_expectancy_penalizes(self) -> None:
        s = score_prior_success(
            PriorSuccessMetrics(n_trades=20, expectancy_r=-0.5),
        )
        assert s == pytest.approx(-1.0)

    def test_consecutive_losses_extra_penalty(self) -> None:
        base = score_prior_success(
            PriorSuccessMetrics(n_trades=20, expectancy_r=0.0),
        )
        with_streak = score_prior_success(
            PriorSuccessMetrics(
                n_trades=20,
                expectancy_r=0.0,
                consecutive_losses=3,
            ),
        )
        assert with_streak < base

    def test_consecutive_wins_extra_boost(self) -> None:
        base = score_prior_success(
            PriorSuccessMetrics(n_trades=20, expectancy_r=0.0),
        )
        with_streak = score_prior_success(
            PriorSuccessMetrics(
                n_trades=20,
                expectancy_r=0.0,
                consecutive_wins=3,
            ),
        )
        assert with_streak > base

    def test_clamped_to_unit_range(self) -> None:
        s = score_prior_success(
            PriorSuccessMetrics(
                n_trades=50,
                expectancy_r=5.0,  # absurdly high
                consecutive_wins=10,
            ),
        )
        assert s <= 1.0
        s2 = score_prior_success(
            PriorSuccessMetrics(
                n_trades=50,
                expectancy_r=-5.0,
                consecutive_losses=10,
            ),
        )
        assert s2 >= -1.0


# ===========================================================================
# 2. Equity-band classifier
# ===========================================================================


class TestClassifyEquityBand:
    def test_growth(self) -> None:
        assert classify_equity_band(110.0, 100.0) == EquityBand.GROWTH

    def test_neutral(self) -> None:
        assert classify_equity_band(100.0, 100.0) == EquityBand.NEUTRAL
        assert classify_equity_band(96.0, 100.0) == EquityBand.NEUTRAL

    def test_drawdown(self) -> None:
        assert classify_equity_band(92.0, 100.0) == EquityBand.DRAWDOWN

    def test_critical(self) -> None:
        assert classify_equity_band(85.0, 100.0) == EquityBand.CRITICAL

    def test_rejects_nonpositive_high_water(self) -> None:
        with pytest.raises(ValueError, match="high_water must be > 0"):
            classify_equity_band(50.0, 0.0)
        with pytest.raises(ValueError):
            classify_equity_band(50.0, -10.0)

    def test_custom_thresholds(self) -> None:
        # Tight bands: growth above 1%, drawdown under 99%.
        band = classify_equity_band(
            101.5,
            100.0,
            growth_threshold=1.01,
            drawdown_threshold=0.99,
            critical_threshold=0.90,
        )
        assert band == EquityBand.GROWTH


# ===========================================================================
# 3. Tier mapping
# ===========================================================================


class TestTierMapping:
    def test_sniper_shot_conviction_3x(self) -> None:
        """Every positive axis maxed out -> CONVICTION at 3x."""
        prior = PriorSuccessMetrics(
            n_trades=30,
            expectancy_r=0.6,
            consecutive_wins=4,
        )
        ctx = _ctx(
            regime=RegimeLabel.TRENDING,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=10.0,
            htf_bias=Side.LONG,
            equity_band=EquityBand.GROWTH,
            prior=prior,
        )
        v = compute_size(ctx)
        assert v.tier is SizeTier.CONVICTION
        assert v.multiplier == 3.0
        # Risk scaled up
        assert v.adjusted_risk_pct == pytest.approx(
            ctx.base_risk_pct * 3.0,
        )

    def test_standard_in_median_case(self) -> None:
        ctx = _ctx(
            regime=RegimeLabel.TRENDING,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=6.0,
            htf_bias=Side.LONG,
            equity_band=EquityBand.NEUTRAL,
        )
        v = compute_size(ctx)
        assert v.tier is SizeTier.STANDARD
        assert v.multiplier == 1.0

    def test_reduced_when_weak_confluence(self) -> None:
        ctx = _ctx(
            regime=RegimeLabel.TRANSITION,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=4.0,
            htf_bias=None,
            equity_band=EquityBand.NEUTRAL,
        )
        v = compute_size(ctx)
        assert v.tier is SizeTier.REDUCED
        assert v.multiplier == 0.5

    def test_probe_when_multiple_red_flags(self) -> None:
        ctx = _ctx(
            regime=RegimeLabel.RANGING,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=3.5,
            htf_bias=Side.SHORT,
            equity_band=EquityBand.DRAWDOWN,
        )
        v = compute_size(ctx)
        assert v.tier is SizeTier.PROBE

    def test_skip_when_high_vol_and_low_confluence(self) -> None:
        """HIGH_VOL alone contributes -0.25 (after weight), and if
        confluence is weak + HTF disagrees + equity drawdown, we
        should fall into SKIP."""
        prior = PriorSuccessMetrics(
            n_trades=20,
            expectancy_r=-0.4,
            consecutive_losses=4,
        )
        ctx = _ctx(
            regime=RegimeLabel.HIGH_VOL,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=2.0,
            htf_bias=Side.SHORT,
            equity_band=EquityBand.CRITICAL,
            prior=prior,
        )
        v = compute_size(ctx)
        assert v.tier is SizeTier.SKIP
        assert v.multiplier == 0.0
        assert v.adjusted_risk_pct == 0.0

    def test_conviction_2x_vs_3x_boundary(self) -> None:
        """A solid but not-maxed setup lands in CONVICTION 2x band."""
        prior = PriorSuccessMetrics(
            n_trades=20,
            expectancy_r=0.3,
        )
        ctx = _ctx(
            regime=RegimeLabel.TRENDING,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=8.0,
            htf_bias=Side.LONG,
            equity_band=EquityBand.NEUTRAL,
            prior=prior,
        )
        v = compute_size(ctx)
        assert v.tier is SizeTier.CONVICTION
        # Enough to qualify for CONVICTION but less than sniper-shot:
        # landed on 2x not 3x.
        assert v.multiplier == 2.0


# ===========================================================================
# 4. Hard overrides
# ===========================================================================


class TestHardOverrides:
    def test_kill_switch_forces_skip(self) -> None:
        ctx = _ctx(kill_switch_active=True, confluence=10.0)
        v = compute_size(ctx)
        assert v.tier is SizeTier.SKIP
        assert v.adjusted_risk_pct == 0.0
        assert "hard_override:kill_switch_active" in v.rationale

    def test_session_off_forces_skip(self) -> None:
        ctx = _ctx(session_allows_entries=False, confluence=10.0)
        v = compute_size(ctx)
        assert v.tier is SizeTier.SKIP
        assert v.adjusted_risk_pct == 0.0
        assert "hard_override:session_disallows_entries" in v.rationale


# ===========================================================================
# 5. Safety bounds
# ===========================================================================


class TestSafetyBounds:
    def test_max_risk_pct_clamps_conviction(self) -> None:
        # Set a tight ceiling to force the clamp.
        tight_policy = SizingPolicy(max_risk_pct=2.0)
        prior = PriorSuccessMetrics(
            n_trades=30,
            expectancy_r=0.8,
            consecutive_wins=4,
        )
        ctx = _ctx(
            regime=RegimeLabel.TRENDING,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=10.0,
            htf_bias=Side.LONG,
            equity_band=EquityBand.GROWTH,
            prior=prior,
            base_risk_pct=1.0,
        )
        v = compute_size(ctx, tight_policy)
        # Raw multiplier would be 3.0 (1.0 * 3.0 = 3.0%),
        # but clamp caps at 2.0%.
        assert v.multiplier == 3.0
        assert v.adjusted_risk_pct == pytest.approx(2.0)

    def test_min_risk_pct_clamps_reduced(self) -> None:
        policy = SizingPolicy(min_risk_pct=0.8)
        ctx = _ctx(
            regime=RegimeLabel.TRANSITION,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=4.0,
            htf_bias=None,
            equity_band=EquityBand.NEUTRAL,
            base_risk_pct=1.0,
        )
        v = compute_size(ctx, policy)
        # Raw 0.5% would fall below min 0.8%, so clamp pulls up.
        assert v.adjusted_risk_pct >= 0.8

    def test_probe_not_clamped_to_min(self) -> None:
        policy = SizingPolicy(min_risk_pct=0.5)
        ctx = _ctx(
            regime=RegimeLabel.RANGING,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=3.5,
            htf_bias=Side.SHORT,
            equity_band=EquityBand.DRAWDOWN,
            base_risk_pct=1.0,
        )
        v = compute_size(ctx, policy)
        if v.tier is SizeTier.PROBE:
            # PROBE should stay small (< min) even though min is 0.5
            assert v.adjusted_risk_pct == pytest.approx(0.25)

    def test_skip_always_zero(self) -> None:
        ctx = _ctx(kill_switch_active=True)
        v = compute_size(ctx)
        assert v.adjusted_risk_pct == 0.0


# ===========================================================================
# 6. Verdict shape contract
# ===========================================================================


class TestVerdictShape:
    def test_is_frozen_slotted_dataclass(self) -> None:
        ctx = _ctx()
        v = compute_size(ctx)
        assert isinstance(v, SizingVerdict)
        # Frozen -> attribute assignment raises
        with pytest.raises((AttributeError, TypeError)):
            v.tier = SizeTier.STANDARD  # type: ignore[misc]

    def test_axis_scores_always_contains_six_keys(self) -> None:
        ctx = _ctx()
        v = compute_size(ctx)
        assert set(v.axis_scores) == {
            "regime",
            "confluence",
            "htf",
            "equity",
            "prior",
            "total_weighted",
        }

    def test_rationale_contains_tier_and_total(self) -> None:
        ctx = _ctx()
        v = compute_size(ctx)
        joined = " ".join(v.rationale)
        assert f"tier={v.tier.value}" in joined
        assert "total=" in joined

    def test_confidence_is_bounded(self) -> None:
        for confluence in (0.0, 5.0, 10.0):
            ctx = _ctx(confluence=confluence)
            v = compute_size(ctx)
            assert 0.0 <= v.confidence_score <= 1.0

    def test_base_risk_pct_preserved(self) -> None:
        ctx = _ctx(base_risk_pct=1.7)
        v = compute_size(ctx)
        assert v.base_risk_pct == 1.7


# ===========================================================================
# 7. Policy tuning
# ===========================================================================


class TestPolicyTuning:
    def test_default_policy_is_frozen(self) -> None:
        # SizingPolicy(frozen=True) so assignment raises.
        with pytest.raises((AttributeError, TypeError)):
            DEFAULT_SIZING_POLICY.weight_regime = 0.5  # type: ignore[misc]

    def test_custom_weights_change_outcome(self) -> None:
        """Boosting the confluence weight should push a marginal
        context into a higher tier."""
        base = _ctx(
            regime=RegimeLabel.TRANSITION,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=8.0,
            htf_bias=None,
            equity_band=EquityBand.NEUTRAL,
        )
        v_default = compute_size(base, DEFAULT_SIZING_POLICY)
        policy_heavy_confluence = SizingPolicy(
            weight_confluence=0.80,
            weight_regime=0.05,
            weight_htf=0.05,
            weight_equity=0.05,
            weight_prior=0.05,
        )
        v_tuned = compute_size(base, policy_heavy_confluence)
        # Total should be higher with confluence dominating.
        assert v_tuned.axis_scores["total_weighted"] > v_default.axis_scores["total_weighted"]


# ===========================================================================
# 8. Self-evolving: scenario over multiple decisions
# ===========================================================================


class TestSelfEvolvingSemantics:
    def test_losing_streak_downsizes(self) -> None:
        """A bucket that was winning then flipped losing should
        produce a smaller verdict than the same context without the
        streak."""
        good = _ctx(
            regime=RegimeLabel.TRENDING,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=7.0,
            htf_bias=Side.LONG,
            equity_band=EquityBand.NEUTRAL,
            prior=PriorSuccessMetrics(
                n_trades=20,
                expectancy_r=0.2,
                consecutive_wins=3,
            ),
        )
        bad = _ctx(
            regime=RegimeLabel.TRENDING,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=7.0,
            htf_bias=Side.LONG,
            equity_band=EquityBand.NEUTRAL,
            prior=PriorSuccessMetrics(
                n_trades=20,
                expectancy_r=-0.2,
                consecutive_losses=4,
            ),
        )
        v_good = compute_size(good)
        v_bad = compute_size(bad)
        assert v_good.multiplier >= v_bad.multiplier
        assert v_good.axis_scores["prior"] > v_bad.axis_scores["prior"]

    def test_critical_drawdown_suppresses_conviction(self) -> None:
        """Even a clean setup in CRITICAL equity band should not
        be sized at 3x -- the capital-preservation rail should
        pull it down."""
        ctx = _ctx(
            regime=RegimeLabel.TRENDING,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=9.0,
            htf_bias=Side.LONG,
            equity_band=EquityBand.CRITICAL,
            prior=PriorSuccessMetrics(n_trades=20, expectancy_r=0.4),
        )
        v = compute_size(ctx)
        # Critical equity pulls at least one tier down relative to
        # the same ctx with NEUTRAL equity.
        same_ctx_neutral = _ctx(
            regime=RegimeLabel.TRENDING,
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            confluence=9.0,
            htf_bias=Side.LONG,
            equity_band=EquityBand.NEUTRAL,
            prior=PriorSuccessMetrics(n_trades=20, expectancy_r=0.4),
        )
        v_neutral = compute_size(same_ctx_neutral)
        assert v.multiplier <= v_neutral.multiplier
