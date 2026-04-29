from __future__ import annotations

from datetime import UTC, datetime

from eta_engine.brain.jarvis_v3.next_level.debate import (
    Argument,
    Persona,
    bear_argue,
    bull_argue,
    full_debate,
    historian_argue,
    hold_debate,
    skeptic_argue,
)


def test_personas_emit_expected_votes_for_clear_contexts() -> None:
    bull = bull_argue(stress=0.1, sizing_mult=1.0, regime="RISK_ON", suggestion="TRADE")
    bear = bear_argue(stress=0.9, sizing_mult=0.2, regime="CRISIS", suggestion="KILL", dd_pct=0.04)
    skeptic = skeptic_argue(stress=0.45, regime="UNKNOWN", regime_confidence=0.2, events_count=3)
    historian = historian_argue(precedent_n=10, precedent_win_rate=0.7, precedent_mean_r=0.6)

    assert bull.vote == "APPROVE"
    assert bear.vote == "DENY"
    assert skeptic.vote == "DEFER"
    assert historian.vote == "APPROVE"
    assert all(arg.reasons for arg in [bull, bear, skeptic, historian])


def test_hold_debate_normalizes_weighted_scores_and_consensus() -> None:
    now = datetime(2026, 4, 29, tzinfo=UTC)
    verdict = hold_debate(
        [
            Argument(persona=Persona.BULL, vote="APPROVE", confidence=1.0),
            Argument(persona=Persona.BEAR, vote="APPROVE", confidence=1.0),
            Argument(persona=Persona.SKEPTIC, vote="APPROVE", confidence=1.0),
            Argument(persona=Persona.HISTORIAN, vote="DENY", confidence=1.0),
        ],
        now=now,
    )

    assert verdict.ts == now
    assert verdict.final_vote == "APPROVE"
    assert verdict.consensus is True
    assert round(sum(verdict.score.values()), 4) == 1.0
    assert "APPROVE wins" in verdict.summary


def test_full_debate_includes_all_four_personas() -> None:
    verdict = full_debate(
        stress=0.2,
        sizing_mult=0.9,
        regime="NEUTRAL",
        regime_confidence=0.8,
        suggestion="TRADE",
        precedent_n=8,
        precedent_win_rate=0.75,
        precedent_mean_r=0.45,
    )

    assert len(verdict.transcript) == 4
    assert {arg.persona for arg in verdict.transcript} == set(Persona)
    assert verdict.final_vote in {"APPROVE", "CONDITIONAL", "DENY", "DEFER"}
