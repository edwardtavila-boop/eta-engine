"""
JARVIS v3 // next_level.self_play
=================================
Adversarial self-play sandbox.

An internal "red market" generator actively tries to break JARVIS's
verdicts (regime flips, liquidity crashes, spoofing patterns). JARVIS
plays against it and learns -- the way AlphaZero learns by self-play
against past versions of itself.

This module provides:

  * ``MarketEvent``       -- a synthetic adversarial event
  * ``RedMarket``         -- adversarial event generator
  * ``SelfPlayRound``     -- one round: RedMarket event -> JARVIS verdict
                             -> realized outcome
  * ``SelfPlayLedger``    -- history of rounds for learning
  * ``run_round``         -- play one round; update bandit/preferences

Deterministic given a seed, so experiments are reproducible.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EventKind(StrEnum):
    REGIME_FLIP      = "REGIME_FLIP"          # CRISIS appears out of nowhere
    LIQUIDITY_CRASH  = "LIQUIDITY_CRASH"      # spreads blow out
    FEED_STUCK       = "FEED_STUCK"           # regime_confidence frozen
    HIDDEN_MOMENTUM  = "HIDDEN_MOMENTUM"      # real edge JARVIS may miss
    SPOOFING         = "SPOOFING"             # false signal ambush
    MACRO_SHOCK      = "MACRO_SHOCK"          # surprise FOMC / CPI
    SESSION_OUTLIER  = "SESSION_OUTLIER"      # unusual session-phase regime


class MarketEvent(BaseModel):
    """One synthetic adversarial event."""
    model_config = ConfigDict(frozen=True)

    ts:            datetime
    kind:          EventKind
    regime_hint:   str  # what the regime classifier would SAY
    truth_regime:  str  # what the regime ACTUALLY is (ground truth)
    stress_pushed: float = Field(ge=0.0, le=1.0)
    realized_r_if_trade: float  # payoff if JARVIS approves
    realized_r_if_deny:  float = 0.0   # always 0 -- denied trades don't pay
    note:          str = ""


class SelfPlayRound(BaseModel):
    """One round of self-play."""
    model_config = ConfigDict(frozen=True)

    round_id:      int = Field(ge=0)
    event:         MarketEvent
    jarvis_verdict: str = Field(min_length=1)
    realized_r:    float
    correct:       bool
    note:          str = ""


class SelfPlaySummary(BaseModel):
    """Roll-up across many rounds."""
    model_config = ConfigDict(frozen=True)

    rounds:        int = Field(ge=0)
    approve_count: int = Field(ge=0)
    deny_count:    int = Field(ge=0)
    correct:       int = Field(ge=0)
    cumulative_r:  float
    win_rate:      float = Field(ge=0.0, le=1.0)
    by_event:      dict[str, float] = Field(default_factory=dict)


class RedMarket:
    """Adversarial event generator. Deterministic when seeded."""

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(seed)
        self._round = 0

    def emit(self) -> MarketEvent:
        self._round += 1
        kind = self._rng.choice(list(EventKind))
        ts = datetime.now(UTC)
        if kind == EventKind.REGIME_FLIP:
            hint = "NEUTRAL"
            truth = "CRISIS"
            stress_pushed = 0.3
            pay_if_trade = -2.5  # approving in hidden crisis = big loss
        elif kind == EventKind.LIQUIDITY_CRASH:
            hint = "RISK_OFF"
            truth = "RISK_OFF"
            stress_pushed = 0.8
            pay_if_trade = -1.5
        elif kind == EventKind.FEED_STUCK:
            hint = "NEUTRAL"
            truth = "RISK_ON"
            stress_pushed = 0.2
            pay_if_trade = self._rng.uniform(-0.5, +0.5)  # unknowable
        elif kind == EventKind.HIDDEN_MOMENTUM:
            hint = "NEUTRAL"
            truth = "RISK_ON"
            stress_pushed = 0.2
            pay_if_trade = +2.0  # real edge!
        elif kind == EventKind.SPOOFING:
            hint = "RISK_ON"
            truth = "NEUTRAL"
            stress_pushed = 0.2
            pay_if_trade = -1.0
        elif kind == EventKind.MACRO_SHOCK:
            hint = "NEUTRAL"
            truth = "CRISIS"
            stress_pushed = 0.9
            pay_if_trade = self._rng.uniform(-3.0, +1.0)
        else:  # SESSION_OUTLIER
            hint = "RISK_ON"
            truth = "RISK_OFF"
            stress_pushed = 0.5
            pay_if_trade = -0.5
        return MarketEvent(
            ts=ts, kind=kind, regime_hint=hint, truth_regime=truth,
            stress_pushed=stress_pushed,
            realized_r_if_trade=round(pay_if_trade, 3),
            note=f"synthetic event {self._round}",
        )


class SelfPlayLedger:
    """History of self-play rounds + summary."""

    def __init__(self) -> None:
        self._rounds: list[SelfPlayRound] = []

    def record(self, rnd: SelfPlayRound) -> None:
        self._rounds.append(rnd)

    def rounds(self) -> list[SelfPlayRound]:
        return list(self._rounds)

    def summary(self) -> SelfPlaySummary:
        if not self._rounds:
            return SelfPlaySummary(
                rounds=0, approve_count=0, deny_count=0,
                correct=0, cumulative_r=0.0, win_rate=0.0,
            )
        approve = sum(1 for r in self._rounds if r.jarvis_verdict == "APPROVE")
        deny = sum(1 for r in self._rounds if r.jarvis_verdict == "DENY")
        correct = sum(1 for r in self._rounds if r.correct)
        cum_r = sum(r.realized_r for r in self._rounds)
        by_event: dict[str, float] = {}
        for r in self._rounds:
            by_event[r.event.kind.value] = by_event.get(
                r.event.kind.value, 0.0,
            ) + r.realized_r
        return SelfPlaySummary(
            rounds=len(self._rounds),
            approve_count=approve,
            deny_count=deny,
            correct=correct,
            cumulative_r=round(cum_r, 4),
            win_rate=round(correct / len(self._rounds), 4),
            by_event={k: round(v, 4) for k, v in by_event.items()},
        )


def play_round(
    *,
    event: MarketEvent,
    jarvis_decide: callable,  # (event) -> verdict string
    round_id: int,
) -> SelfPlayRound:
    """Play one round. ``jarvis_decide`` is the policy under test."""
    verdict = jarvis_decide(event)
    if verdict == "APPROVE":
        realized = event.realized_r_if_trade
    elif verdict == "DENY":
        realized = event.realized_r_if_deny
    else:
        # CONDITIONAL / DEFER -- half-size if approved later
        realized = event.realized_r_if_trade * 0.5
    correct = (
        (verdict == "APPROVE" and event.realized_r_if_trade > 0) or
        (verdict == "DENY" and event.realized_r_if_trade < 0)
    )
    return SelfPlayRound(
        round_id=round_id, event=event, jarvis_verdict=verdict,
        realized_r=round(realized, 3), correct=correct,
    )


def default_policy(event: MarketEvent) -> str:
    """Baseline policy: DENY if regime_hint is RISK_OFF/CRISIS or stress > 0.5.

    Real callers pass their own policy (e.g. the ApexPredatorCore wrapped
    as a callable). This is the trivial baseline for benchmarking.
    """
    if event.regime_hint in {"RISK_OFF", "CRISIS"}:
        return "DENY"
    if event.stress_pushed > 0.5:
        return "CONDITIONAL"
    return "APPROVE"
