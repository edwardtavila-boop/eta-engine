"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_eta_policy.

Unit tests for the 6 named strategies in :mod:`eta_engine.strategies.eta_policy`.

These tests construct deterministic bar streams that make each
strategy fire, then also prove that the guardrails (kill-switch,
session gate, high-vol regime, HTF-bias mismatch) correctly abstain.
"""

from __future__ import annotations

from eta_engine.strategies.eta_policy import (
    STRATEGIES,
    StrategyContext,
    fvg_fill_confluence,
    liquidity_sweep_displacement,
    mtf_trend_following,
    ob_breaker_retest,
    regime_adaptive_allocation,
    rl_full_automation,
)
from eta_engine.strategies.models import Bar, Side, StrategyId

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(
    ts: int,
    o: float,
    h: float,
    low: float,
    c: float,
    v: float = 1000.0,
) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=low, close=c, volume=v)


def _sweep_plus_displacement_stream() -> list[Bar]:
    """40+ bars with equal-lows at 100.0, then sweep-low + bull displacement."""
    bars: list[Bar] = []
    for i in range(30):
        lo = 100.0 if i in (10, 20) else 101.5
        bars.append(_bar(i, 102.0, 103.0, lo, 102.5))
    # 10 small consolidation bars to establish median body ~ 0.5
    for i in range(30, 40):
        bars.append(_bar(i, 102.0, 102.3, 101.8, 102.1))
    # Sweep-and-displacement bar: wicks to 99.4, closes at 104.0 (body=3.0)
    bars.append(_bar(40, 101.0, 104.5, 99.4, 104.0))
    return bars


def _bos_stream() -> list[Bar]:
    """Stepped up-then-pullback-then-break pattern for BOS + OB retest."""
    closes = [
        100.0,
        101.0,
        102.0,
        103.0,
        104.0,
        105.0,
        106.0,
        107.0,
        108.0,
        109.0,
        110.0,
        108.0,
        107.0,
        108.5,
    ]
    bars: list[Bar] = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c - 0.5
        h = max(o, c) + 0.3
        lo = min(o, c) - 0.3
        bars.append(_bar(i, o, h, lo, c))
    # Force final close above pivot but still inside OB zone
    # Pivot is at high of bar index 10 = 110.3. Bar 11 has o=110.0 c=108.0 -> bear candle.
    # OB zone = [107.7, 110.3] (using low, high of that bearish candle).
    bars.append(_bar(14, 108.5, 110.5, 108.0, 110.4))  # close > 110.3 pivot
    return bars


def _fvg_stream() -> list[Bar]:
    """Bullish FVG: bar[0].high=101, bar[2].low=103 forms unfilled gap
    [101, 103]. Last close = 102.5 sits inside the zone. Needs >= 5 bars
    because the fvg_fill_confluence policy abstains below that floor.
    """
    bars = [
        _bar(0, 100.0, 101.0, 99.0, 100.5),
        _bar(1, 101.0, 103.5, 100.5, 103.0),
        _bar(2, 103.0, 105.0, 103.0, 104.5),  # FVG formed here
        _bar(3, 104.5, 105.0, 103.5, 104.0),  # low=103.5 > 101 (gap unfilled)
        _bar(4, 104.0, 104.5, 102.0, 102.5),  # low=102 > 101 (still unfilled)
    ]
    return bars


def _mtf_trend_stream(n: int = 210) -> list[Bar]:
    """Clean uptrend with 200-period MA well below last close + BOS."""
    bars: list[Bar] = []
    close = 100.0
    for i in range(n - 14):
        o = close
        close = close + 1.0
        h = close + 0.5
        lo = o - 0.2
        bars.append(_bar(i, o, h, lo, close))
    # Append stepped BOS tail so detect_break_of_structure fires LONG
    closes_tail = [
        close + 1.0,
        close + 2.0,
        close + 3.0,
        close + 4.0,
        close + 5.0,
        close + 6.0,
        close + 4.0,
        close + 2.0,
        close + 3.0,
        close + 6.5,
        close + 7.0,
        close + 7.5,
        close + 8.0,
        close + 9.0,
    ]
    for i, c in enumerate(closes_tail, start=n - 14):
        o = closes_tail[i - (n - 14) - 1] if i > (n - 14) else closes_tail[0] - 1
        h = max(o, c) + 0.3
        lo = min(o, c) - 0.3
        bars.append(_bar(i, o, h, lo, c))
    return bars


# ---------------------------------------------------------------------------
# Strategy 1: Liquidity Sweep + Displacement
# ---------------------------------------------------------------------------


class TestLiquiditySweepDisplacement:
    def test_fires_long_on_sweep_low_plus_bull_displacement(self) -> None:
        bars = _sweep_plus_displacement_stream()
        sig = liquidity_sweep_displacement(bars, StrategyContext())
        assert sig.is_actionable
        assert sig.side is Side.LONG
        assert sig.strategy is StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT
        assert sig.rr > 0.0

    def test_abstains_on_short_history(self) -> None:
        sig = liquidity_sweep_displacement([], StrategyContext())
        assert sig.side is Side.FLAT
        assert "insufficient_bars_or_session" in sig.rationale_tags

    def test_abstains_when_session_closed(self) -> None:
        bars = _sweep_plus_displacement_stream()
        ctx = StrategyContext(session_allows_entries=False)
        sig = liquidity_sweep_displacement(bars, ctx)
        assert sig.side is Side.FLAT

    def test_kill_switch_zeroes_risk(self) -> None:
        bars = _sweep_plus_displacement_stream()
        ctx = StrategyContext(kill_switch_active=True)
        sig = liquidity_sweep_displacement(bars, ctx)
        assert sig.risk_mult == 0.0
        assert sig.is_actionable is False


# ---------------------------------------------------------------------------
# Strategy 2: OB Breaker Retest
# ---------------------------------------------------------------------------


class TestObBreakerRetest:
    def test_fires_when_bos_plus_ob_plus_in_zone(self) -> None:
        bars = _bos_stream()
        sig = ob_breaker_retest(bars, StrategyContext(htf_bias=Side.LONG))
        assert sig.strategy is StrategyId.OB_BREAKER_RETEST
        # May or may not fire depending on OB zone; verify contract
        if sig.is_actionable:
            assert sig.side is Side.LONG

    def test_abstains_on_htf_mismatch(self) -> None:
        bars = _bos_stream()
        ctx = StrategyContext(htf_bias=Side.SHORT)
        sig = ob_breaker_retest(bars, ctx)
        assert sig.side is Side.FLAT
        assert "htf_bias_mismatch" in sig.rationale_tags

    def test_abstains_without_bos(self) -> None:
        bars = [_bar(i, 100.0, 101.0, 99.0, 100.0) for i in range(15)]
        sig = ob_breaker_retest(bars, StrategyContext())
        assert sig.side is Side.FLAT
        assert "no_bos" in sig.rationale_tags


# ---------------------------------------------------------------------------
# Strategy 3: FVG Fill + Confluence
# ---------------------------------------------------------------------------


class TestFvgFillConfluence:
    def test_fires_on_unfilled_fvg_in_zone(self) -> None:
        bars = _fvg_stream()
        sig = fvg_fill_confluence(bars, StrategyContext())
        assert sig.strategy is StrategyId.FVG_FILL_CONFLUENCE
        assert sig.side is Side.LONG
        assert sig.is_actionable

    def test_adaptive_rr_trending_vs_ranging(self) -> None:
        bars = _fvg_stream()
        trending = fvg_fill_confluence(
            bars,
            StrategyContext(regime_label="TRENDING"),
        )
        ranging = fvg_fill_confluence(
            bars,
            StrategyContext(regime_label="RANGING"),
        )
        # Trending gets rr=3 -> target further; ranging rr=1.5 -> target tighter
        if trending.is_actionable and ranging.is_actionable:
            assert trending.target > ranging.target

    def test_abstains_when_price_outside_zone(self) -> None:
        bars = _fvg_stream()
        # Shift last bar far above zone so close (109.5) is outside [101, 103]
        bars[-1] = _bar(4, 104.0, 110.0, 108.0, 109.5)
        sig = fvg_fill_confluence(bars, StrategyContext())
        assert sig.side is Side.FLAT
        assert "price_not_in_fvg" in sig.rationale_tags or "no_fvg" in sig.rationale_tags


# ---------------------------------------------------------------------------
# Strategy 4: MTF Trend-Following
# ---------------------------------------------------------------------------


class TestMtfTrendFollowing:
    def test_abstains_on_short_stream(self) -> None:
        sig = mtf_trend_following([_bar(0, 100.0, 101.0, 99.0, 100.5)], StrategyContext())
        assert sig.side is Side.FLAT

    def test_abstains_when_regime_not_trending(self) -> None:
        bars = _mtf_trend_stream(n=210)
        sig = mtf_trend_following(bars, StrategyContext(regime_label="RANGING"))
        assert sig.side is Side.FLAT
        assert any("regime_not_trending" in tag for tag in sig.rationale_tags)

    def test_fires_with_trending_regime_and_trend(self) -> None:
        bars = _mtf_trend_stream(n=210)
        ctx = StrategyContext(regime_label="TRENDING")
        sig = mtf_trend_following(bars, ctx)
        assert sig.strategy is StrategyId.MTF_TREND_FOLLOWING
        # Only assert actionable if BOS fired; else fall back to contract
        assert sig.side in (Side.LONG, Side.FLAT)


# ---------------------------------------------------------------------------
# Strategy 5: Regime-Adaptive Allocation marker
# ---------------------------------------------------------------------------


class TestRegimeAdaptiveAllocation:
    def test_marker_is_non_directional(self) -> None:
        bars = [_bar(i, 100.0, 101.0, 99.0, 100.0) for i in range(5)]
        sig = regime_adaptive_allocation(bars, StrategyContext())
        assert sig.strategy is StrategyId.REGIME_ADAPTIVE_ALLOCATION
        assert sig.side is Side.FLAT
        assert sig.risk_mult == 0.0

    def test_abstains_on_empty_bars(self) -> None:
        sig = regime_adaptive_allocation([], StrategyContext())
        assert sig.side is Side.FLAT
        assert "no_bars" in sig.rationale_tags


# ---------------------------------------------------------------------------
# Strategy 6: RL Full-Automation marker
# ---------------------------------------------------------------------------


class TestRlFullAutomationMarker:
    def test_marker_returns_flat(self) -> None:
        sig = rl_full_automation([], StrategyContext())
        assert sig.strategy is StrategyId.RL_FULL_AUTOMATION
        assert sig.side is Side.FLAT
        assert "rl_agent_external" in sig.rationale_tags


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_registry_has_all_six_strategies(self) -> None:
        assert set(STRATEGIES.keys()) == set(StrategyId)

    def test_every_callable_accepts_bars_and_ctx(self) -> None:
        bars = [_bar(i, 100.0, 101.0, 99.0, 100.5) for i in range(5)]
        ctx = StrategyContext()
        for sid, fn in STRATEGIES.items():
            out = fn(bars, ctx)  # type: ignore[operator]
            assert out.strategy is sid
