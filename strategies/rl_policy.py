"""EVOLUTIONARY TRADING ALGO  //  strategies.rl_policy.

Strategy #6 -- RL full-automation policy bridge.

Wraps the existing :class:`eta_engine.brain.rl_agent.RLAgent` so the
policy router can invoke it through the standard
:class:`StrategySignal` contract. Keeps the strategies package free of
pydantic / torch imports; all heavy machinery lives in
:mod:`eta_engine.brain.rl_agent`.

If the agent is unavailable (no checkpoint, no torch installed,
deterministic-test mode), this module returns a FLAT signal with a tag
explaining why -- never crashes the router.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from eta_engine.strategies.models import (
    Bar,
    Side,
    StrategyId,
    StrategySignal,
)

# ---------------------------------------------------------------------------
# Feature builder
# ---------------------------------------------------------------------------


def _pct(a: float, b: float) -> float:
    if a == 0.0:
        return 0.0
    return (b - a) / abs(a)


def build_feature_vector(bars: list[Bar], *, window: int = 20) -> list[float]:
    """Compact OHLCV feature vector for the RL agent.

    Twelve features by default:
      * trailing return over ``window``
      * trailing vol (std of log returns) over ``window``
      * median body / median range
      * volume z-score vs. window
      * bull-bar fraction
      * max drawdown in window
      * last-bar body / range
      * last-bar close / last-bar high
      * last-bar low / last-bar close
      * last-bar volume / window-median volume
      * trend-slope sign of closes over ``window``
      * ratio of up-closes to down-closes
    """
    if len(bars) < window:
        return [0.0] * 12

    w = bars[-window:]
    closes = [b.close for b in w]
    last = w[-1]

    def _std(xs: list[float]) -> float:
        n = len(xs)
        if n < 2:
            return 0.0
        mean = sum(xs) / n
        var = sum((x - mean) ** 2 for x in xs) / (n - 1)
        return var**0.5

    def _median(xs: list[float]) -> float:
        s = sorted(xs)
        return s[len(s) // 2] if s else 0.0

    rets: list[float] = []
    for i in range(1, len(closes)):
        prev, curr = closes[i - 1], closes[i]
        if prev > 0.0:
            rets.append((curr - prev) / prev)

    volumes = [b.volume for b in w]
    med_vol = _median(volumes)
    vol_z = (last.volume - med_vol) / (_std(volumes) + 1e-9) if med_vol > 0 else 0.0
    bull_frac = sum(1 for b in w if b.is_bull) / len(w)
    max_dd = 0.0
    peak = closes[0]
    for c in closes:
        peak = max(peak, c)
        dd = (peak - c) / max(peak, 1e-9)
        max_dd = max(max_dd, dd)

    body_over_range = last.body / max(last.range, 1e-9)
    close_over_high = last.close / max(last.high, 1e-9)
    low_over_close = last.low / max(last.close, 1e-9)
    vol_over_med = last.volume / max(med_vol, 1e-9) if med_vol > 0 else 0.0

    # Trend slope sign
    slope = _pct(closes[0], closes[-1])

    # Up/down close ratio
    ups = sum(1 for r in rets if r > 0)
    downs = sum(1 for r in rets if r < 0)
    ratio = ups / max(downs, 1)

    return [
        slope,
        _std(rets),
        _median([b.body for b in w]) / max(_median([b.range for b in w]), 1e-9),
        vol_z,
        bull_frac,
        max_dd,
        body_over_range,
        close_over_high,
        low_over_close,
        vol_over_med,
        1.0 if slope > 0 else -1.0 if slope < 0 else 0.0,
        ratio,
    ]


# ---------------------------------------------------------------------------
# Agent protocol (so tests can inject a stub)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RLDecision:
    """Decision emitted by the agent. Kept intentionally thin."""

    action: Side
    confidence: float = 5.0
    risk_mult: float = 1.0
    meta: dict[str, float] = field(default_factory=dict)


class RLAgentProto(Protocol):
    """What an RL agent must implement for this wrapper to call it."""

    def decide(self, features: list[float]) -> RLDecision: ...


# ---------------------------------------------------------------------------
# Default no-op agent (checkpoint unavailable)
# ---------------------------------------------------------------------------


class NullRLAgent:
    """Always abstains. Used when no checkpoint is wired."""

    def decide(self, features: list[float]) -> RLDecision:
        _ = features
        return RLDecision(action=Side.FLAT, confidence=0.0, risk_mult=0.0)


# ---------------------------------------------------------------------------
# Public policy fn
# ---------------------------------------------------------------------------


def rl_policy_signal(
    bars: list[Bar],
    agent: RLAgentProto | None = None,
    *,
    window: int = 20,
    stop_buffer_pct: float = 0.003,
    target_rr: float = 2.0,
) -> StrategySignal:
    """Run the RL agent and wrap its decision as a StrategySignal."""
    strategy = StrategyId.RL_FULL_AUTOMATION
    if len(bars) < window:
        return StrategySignal(
            strategy=strategy,
            side=Side.FLAT,
            rationale_tags=("insufficient_bars",),
        )
    ag: RLAgentProto = agent or NullRLAgent()
    features = build_feature_vector(bars, window=window)
    decision = ag.decide(features)
    if decision.action is Side.FLAT or decision.risk_mult <= 0.0:
        return StrategySignal(
            strategy=strategy,
            side=Side.FLAT,
            rationale_tags=("rl_abstained",),
        )
    last = bars[-1].close
    if decision.action is Side.LONG:
        stop = last * (1.0 - stop_buffer_pct)
        target = last + (last - stop) * target_rr
    else:
        stop = last * (1.0 + stop_buffer_pct)
        target = last - (stop - last) * target_rr
    return StrategySignal(
        strategy=strategy,
        side=decision.action,
        entry=last,
        stop=stop,
        target=target,
        confidence=max(0.0, min(10.0, decision.confidence)),
        risk_mult=max(0.0, min(1.5, decision.risk_mult)),
        rationale_tags=("rl_policy",),
        meta={**decision.meta, "stop_buffer_pct": stop_buffer_pct},
    )
