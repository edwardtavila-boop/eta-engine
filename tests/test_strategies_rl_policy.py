"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_rl_policy.

Unit tests for the RL policy wrapper.

The real PPO agent is replaced with a stub implementing
:class:`RLAgentProto` so tests stay hermetic (no torch, no checkpoint).
"""

from __future__ import annotations

import pytest

from eta_engine.strategies.models import Bar, Side, StrategyId
from eta_engine.strategies.rl_policy import (
    NullRLAgent,
    RLDecision,
    build_feature_vector,
    rl_policy_signal,
)

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


def _uptrend(n: int = 25) -> list[Bar]:
    bars: list[Bar] = []
    close = 100.0
    for i in range(n):
        o = close
        close += 1.0
        h = close + 0.2
        lo = o - 0.2
        bars.append(_bar(i, o, h, lo, close))
    return bars


class _FixedAgent:
    """Returns a pre-baked decision regardless of features."""

    def __init__(self, decision: RLDecision) -> None:
        self._decision = decision

    def decide(self, features: list[float]) -> RLDecision:
        _ = features
        return self._decision


# ---------------------------------------------------------------------------
# build_feature_vector
# ---------------------------------------------------------------------------


class TestBuildFeatureVector:
    def test_returns_zeros_when_short(self) -> None:
        bars = _uptrend(n=5)
        fv = build_feature_vector(bars, window=20)
        assert fv == [0.0] * 12

    def test_returns_12_features(self) -> None:
        bars = _uptrend(n=25)
        fv = build_feature_vector(bars, window=20)
        assert len(fv) == 12

    def test_slope_sign_positive_in_uptrend(self) -> None:
        bars = _uptrend(n=25)
        fv = build_feature_vector(bars, window=20)
        # feature index 10 = slope sign
        assert fv[10] == 1.0

    def test_slope_sign_negative_in_downtrend(self) -> None:
        bars: list[Bar] = []
        close = 200.0
        for i in range(25):
            o = close
            close -= 1.0
            h = o + 0.2
            lo = close - 0.2
            bars.append(_bar(i, o, h, lo, close))
        fv = build_feature_vector(bars, window=20)
        assert fv[10] == -1.0


# ---------------------------------------------------------------------------
# RLDecision
# ---------------------------------------------------------------------------


class TestRLDecision:
    def test_defaults(self) -> None:
        d = RLDecision(action=Side.LONG)
        assert d.confidence == 5.0
        assert d.risk_mult == 1.0
        assert d.meta == {}

    def test_frozen(self) -> None:
        d = RLDecision(action=Side.LONG)
        with pytest.raises(AttributeError):
            d.action = Side.SHORT  # type: ignore[misc]


# ---------------------------------------------------------------------------
# NullRLAgent
# ---------------------------------------------------------------------------


class TestNullRLAgent:
    def test_abstains(self) -> None:
        agent = NullRLAgent()
        decision = agent.decide([0.0] * 12)
        assert decision.action is Side.FLAT
        assert decision.risk_mult == 0.0

    def test_has_decide_method(self) -> None:
        agent = NullRLAgent()
        # Structural protocol check: the wrapper only needs ``decide``
        assert callable(agent.decide)
        out = agent.decide([0.0] * 12)
        assert isinstance(out, RLDecision)


# ---------------------------------------------------------------------------
# rl_policy_signal
# ---------------------------------------------------------------------------


class TestRlPolicySignal:
    def test_abstains_on_short_stream(self) -> None:
        bars = _uptrend(n=5)
        sig = rl_policy_signal(bars, window=20)
        assert sig.strategy is StrategyId.RL_FULL_AUTOMATION
        assert sig.side is Side.FLAT
        assert "insufficient_bars" in sig.rationale_tags

    def test_abstains_when_agent_returns_flat(self) -> None:
        bars = _uptrend(n=25)
        agent = _FixedAgent(RLDecision(action=Side.FLAT, risk_mult=0.0))
        sig = rl_policy_signal(bars, agent=agent)
        assert sig.side is Side.FLAT
        assert "rl_abstained" in sig.rationale_tags

    def test_abstains_when_risk_mult_zero(self) -> None:
        bars = _uptrend(n=25)
        agent = _FixedAgent(RLDecision(action=Side.LONG, risk_mult=0.0))
        sig = rl_policy_signal(bars, agent=agent)
        assert sig.side is Side.FLAT

    def test_fires_long_from_agent(self) -> None:
        bars = _uptrend(n=25)
        agent = _FixedAgent(
            RLDecision(action=Side.LONG, confidence=7.5, risk_mult=1.25),
        )
        sig = rl_policy_signal(
            bars,
            agent=agent,
            stop_buffer_pct=0.003,
            target_rr=2.0,
        )
        assert sig.is_actionable
        assert sig.side is Side.LONG
        assert sig.strategy is StrategyId.RL_FULL_AUTOMATION
        last_close = bars[-1].close
        assert sig.entry == pytest.approx(last_close)
        # stop should be below entry, target above; R:R ~= 2
        assert sig.stop < sig.entry
        assert sig.target > sig.entry
        assert sig.rr == pytest.approx(2.0, rel=0.01)

    def test_fires_short_from_agent(self) -> None:
        bars = _uptrend(n=25)
        agent = _FixedAgent(
            RLDecision(action=Side.SHORT, confidence=4.0, risk_mult=0.8),
        )
        sig = rl_policy_signal(bars, agent=agent)
        assert sig.is_actionable
        assert sig.side is Side.SHORT
        assert sig.stop > sig.entry
        assert sig.target < sig.entry

    def test_clamps_confidence_to_0_10(self) -> None:
        bars = _uptrend(n=25)
        agent = _FixedAgent(
            RLDecision(action=Side.LONG, confidence=99.0, risk_mult=1.0),
        )
        sig = rl_policy_signal(bars, agent=agent)
        assert sig.confidence == 10.0

    def test_clamps_risk_mult_to_1p5(self) -> None:
        bars = _uptrend(n=25)
        agent = _FixedAgent(
            RLDecision(action=Side.LONG, confidence=5.0, risk_mult=99.0),
        )
        sig = rl_policy_signal(bars, agent=agent)
        assert sig.risk_mult == 1.5

    def test_meta_propagates_agent_meta(self) -> None:
        bars = _uptrend(n=25)
        agent = _FixedAgent(
            RLDecision(
                action=Side.LONG,
                confidence=5.0,
                risk_mult=1.0,
                meta={"policy_value": 0.74},
            ),
        )
        sig = rl_policy_signal(bars, agent=agent)
        assert sig.meta["policy_value"] == pytest.approx(0.74)
        assert "stop_buffer_pct" in sig.meta

    def test_null_agent_default_abstains(self) -> None:
        bars = _uptrend(n=25)
        sig = rl_policy_signal(bars)  # no agent -> NullRLAgent
        assert sig.side is Side.FLAT
