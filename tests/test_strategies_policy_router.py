"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_policy_router.

Unit tests for :mod:`eta_engine.strategies.policy_router` -- the
per-asset dispatcher that runs every eligible strategy and picks the
highest-scoring candidate.
"""

from __future__ import annotations

from eta_engine.strategies.eta_policy import StrategyContext
from eta_engine.strategies.models import Bar, Side, StrategyId, StrategySignal
from eta_engine.strategies.policy_router import (
    DEFAULT_ELIGIBILITY,
    RouterDecision,
    dispatch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(ts: int, close: float = 100.0) -> Bar:
    return Bar(ts=ts, open=close, high=close + 1, low=close - 1, close=close)


def _stub(
    sid: StrategyId,
    side: Side = Side.LONG,
    confidence: float = 5.0,
    risk_mult: float = 1.0,
) -> StrategySignal:
    return StrategySignal(
        strategy=sid,
        side=side,
        entry=100.0,
        stop=95.0,
        target=110.0,
        confidence=confidence,
        risk_mult=risk_mult,
    )


# ---------------------------------------------------------------------------
# DEFAULT_ELIGIBILITY
# ---------------------------------------------------------------------------


class TestDefaultEligibility:
    def test_mnq_runs_four_strategies(self) -> None:
        eligible = DEFAULT_ELIGIBILITY["MNQ"]
        assert StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT in eligible
        assert StrategyId.OB_BREAKER_RETEST in eligible
        assert StrategyId.FVG_FILL_CONFLUENCE in eligible
        assert StrategyId.MTF_TREND_FOLLOWING in eligible
        assert StrategyId.RL_FULL_AUTOMATION not in eligible

    def test_btc_runs_rl(self) -> None:
        eligible = DEFAULT_ELIGIBILITY["BTC"]
        assert StrategyId.RL_FULL_AUTOMATION in eligible

    def test_portfolio_runs_allocator(self) -> None:
        eligible = DEFAULT_ELIGIBILITY["PORTFOLIO"]
        assert eligible == (StrategyId.REGIME_ADAPTIVE_ALLOCATION,)


# ---------------------------------------------------------------------------
# RouterDecision
# ---------------------------------------------------------------------------


class TestRouterDecision:
    def test_fired_count_ignores_flat(self) -> None:
        winner = _stub(StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT)
        other = StrategySignal(
            strategy=StrategyId.OB_BREAKER_RETEST,
            side=Side.FLAT,
        )
        rd = RouterDecision(
            asset="MNQ",
            winner=winner,
            candidates=(winner, other),
        )
        assert rd.fired_count == 1

    def test_as_dict_is_json_safe(self) -> None:
        winner = _stub(StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT)
        rd = RouterDecision(
            asset="MNQ",
            winner=winner,
            candidates=(winner,),
            eligible=(StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,),
        )
        d = rd.as_dict()
        assert d["asset"] == "MNQ"
        assert d["fired_count"] == 1
        assert d["eligible"] == ["liquidity_sweep_displacement"]
        assert d["winner"]["strategy"] == "liquidity_sweep_displacement"


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_picks_highest_confidence(self) -> None:
        bars = [_bar(i) for i in range(10)]
        ctx = StrategyContext()
        # Build a fake registry with one high-confidence winner
        registry = {
            StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: (
                lambda b, c: _stub(
                    StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                    confidence=3.0,
                )
            ),
            StrategyId.OB_BREAKER_RETEST: (
                lambda b, c: _stub(
                    StrategyId.OB_BREAKER_RETEST,
                    confidence=9.0,
                )
            ),
            StrategyId.FVG_FILL_CONFLUENCE: (
                lambda b, c: _stub(
                    StrategyId.FVG_FILL_CONFLUENCE,
                    confidence=5.0,
                )
            ),
            StrategyId.MTF_TREND_FOLLOWING: (
                lambda b, c: _stub(
                    StrategyId.MTF_TREND_FOLLOWING,
                    confidence=1.0,
                )
            ),
        }
        decision = dispatch("MNQ", bars, ctx, registry=registry)
        assert decision.winner.strategy is StrategyId.OB_BREAKER_RETEST
        assert len(decision.candidates) == 4
        assert decision.fired_count == 4

    def test_risk_mult_tiebreak(self) -> None:
        bars = [_bar(i) for i in range(10)]
        ctx = StrategyContext()
        registry = {
            StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: (
                lambda b, c: _stub(
                    StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                    confidence=5.0,
                    risk_mult=0.5,
                )
            ),
            StrategyId.OB_BREAKER_RETEST: (
                lambda b, c: _stub(
                    StrategyId.OB_BREAKER_RETEST,
                    confidence=5.0,
                    risk_mult=1.25,
                )
            ),
        }
        eligibility = {
            "MNQ": (
                StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                StrategyId.OB_BREAKER_RETEST,
            ),
        }
        decision = dispatch(
            "MNQ",
            bars,
            ctx,
            registry=registry,
            eligibility=eligibility,
        )
        assert decision.winner.strategy is StrategyId.OB_BREAKER_RETEST

    def test_flat_signals_never_win(self) -> None:
        bars = [_bar(i) for i in range(10)]
        ctx = StrategyContext()
        registry = {
            StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: (
                lambda b, c: StrategySignal(
                    strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                    side=Side.FLAT,
                    confidence=9.9,
                    risk_mult=1.5,
                )
            ),
            StrategyId.OB_BREAKER_RETEST: (
                lambda b, c: _stub(
                    StrategyId.OB_BREAKER_RETEST,
                    confidence=0.5,
                )
            ),
        }
        eligibility = {
            "MNQ": (
                StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                StrategyId.OB_BREAKER_RETEST,
            ),
        }
        decision = dispatch(
            "MNQ",
            bars,
            ctx,
            registry=registry,
            eligibility=eligibility,
        )
        # The FLAT signal scores 0.0 per the _score function contract.
        # The winner must be the actionable one even with lower confidence.
        assert decision.winner.strategy is StrategyId.OB_BREAKER_RETEST

    def test_unknown_asset_uses_fallback(self) -> None:
        bars = [_bar(i) for i in range(10)]
        ctx = StrategyContext()
        decision = dispatch("UNKNOWN", bars, ctx)
        assert decision.asset == "UNKNOWN"
        assert len(decision.eligible) == 4

    def test_missing_strategy_in_registry_skipped(self) -> None:
        bars = [_bar(i) for i in range(10)]
        ctx = StrategyContext()
        registry = {
            StrategyId.OB_BREAKER_RETEST: (
                lambda b, c: _stub(
                    StrategyId.OB_BREAKER_RETEST,
                    confidence=5.0,
                )
            ),
        }
        eligibility = {
            "MNQ": (
                StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,  # missing in registry
                StrategyId.OB_BREAKER_RETEST,
            ),
        }
        decision = dispatch(
            "MNQ",
            bars,
            ctx,
            registry=registry,
            eligibility=eligibility,
        )
        assert len(decision.candidates) == 1
        assert decision.winner.strategy is StrategyId.OB_BREAKER_RETEST

    def test_asset_symbol_is_upper_cased(self) -> None:
        bars = [_bar(i) for i in range(10)]
        decision = dispatch("mnq", bars, StrategyContext())
        assert decision.asset == "MNQ"

    def test_no_candidates_returns_flat(self) -> None:
        bars = [_bar(i) for i in range(10)]
        ctx = StrategyContext()
        decision = dispatch(
            "MNQ",
            bars,
            ctx,
            registry={},
            eligibility={"MNQ": ()},
        )
        assert decision.winner.side is Side.FLAT
        assert "no_candidates" in decision.winner.rationale_tags


# ---------------------------------------------------------------------------
# Integration with real registry
# ---------------------------------------------------------------------------


class TestRealRegistryIntegration:
    def test_mnq_dispatch_with_flat_bars_returns_decision(self) -> None:
        """Even with boring bars every strategy still returns *some* signal."""
        bars = [Bar(ts=i, open=100, high=101, low=99, close=100) for i in range(50)]
        decision = dispatch("MNQ", bars, StrategyContext())
        assert isinstance(decision, RouterDecision)
        # All 4 MNQ-eligible strategies should have been consulted
        assert len(decision.candidates) == 4
