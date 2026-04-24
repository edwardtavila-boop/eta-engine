"""
JARVIS v3 // next_level.debate
==============================
Multi-persona internal debate engine.

Before any non-trivial verdict, four internal personas argue the case:

  * BULL      -- reasons to trade / approve / stay long
  * BEAR      -- reasons to stand aside / deny / get flat
  * SKEPTIC   -- "what am I missing?" devil's advocate on both sides
  * HISTORIAN -- pulls precedent; "last N times this setup fired, what happened?"

Each persona emits an ``Argument`` (verdict, confidence, reasons). A simple
weighted-vote aggregator produces the debate verdict. The full transcript
is logged so the operator can scroll any decision's debate.

Design: personas are pure functions over the context + supporting data.
No LLM call -- this is deterministic argumentation based on the rules each
persona represents. (Future: a hybrid mode where operator toggles Claude-
backed reasoning per persona via the model_policy / bandit.)
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Persona(StrEnum):
    BULL      = "BULL"
    BEAR      = "BEAR"
    SKEPTIC   = "SKEPTIC"
    HISTORIAN = "HISTORIAN"


class Argument(BaseModel):
    """One persona's contribution to the debate."""
    model_config = ConfigDict(frozen=True)

    persona:    Persona
    vote:       str   = Field(pattern="^(APPROVE|CONDITIONAL|DENY|DEFER)$")
    confidence: float = Field(ge=0.0, le=1.0)
    reasons:    list[str] = Field(default_factory=list)
    evidence:   list[str] = Field(default_factory=list)


class DebateVerdict(BaseModel):
    """Aggregated verdict from the debate."""
    model_config = ConfigDict(frozen=True)

    ts:            datetime
    final_vote:    str = Field(min_length=1)
    score:         dict[str, float]     # APPROVE/CONDITIONAL/DENY/DEFER -> weight
    majority_by:   float = Field(ge=0.0, le=1.0)
    transcript:    list[Argument]
    summary:       str
    consensus:     bool  # True if >=3 of 4 personas agreed


# Persona vote weights -- tunable. Historian gets a slightly higher weight
# because it brings hard evidence (past outcomes). Skeptic is a balance.
DEFAULT_WEIGHTS: dict[Persona, float] = {
    Persona.BULL:      1.0,
    Persona.BEAR:      1.0,
    Persona.SKEPTIC:   1.0,
    Persona.HISTORIAN: 1.3,
}


# ---------------------------------------------------------------------------
# Persona implementations
# ---------------------------------------------------------------------------

def bull_argue(
    *, stress: float, sizing_mult: float, regime: str, suggestion: str,
) -> Argument:
    """Optimist: "edges exist; this is what the bot is built for"."""
    reasons: list[str] = []
    evidence: list[str] = []
    if stress < 0.4:
        reasons.append(f"stress low ({stress:.0%}) -- regime supports entry")
    if sizing_mult >= 0.7:
        reasons.append(f"sizing intact (mult={sizing_mult:.0%})")
    if regime.upper() in {"RISK_ON", "RISK-ON", "NEUTRAL"}:
        reasons.append(f"regime={regime} -- not hostile")
    if suggestion in {"TRADE", "REVIEW"}:
        reasons.append(f"v2 suggestion={suggestion} -- edge alive")
    conf = 0.5 + 0.4 * (1.0 - stress) + 0.1 * sizing_mult
    vote = "APPROVE" if conf > 0.7 else "CONDITIONAL"
    evidence.append("history: edges only pay if taken")
    return Argument(
        persona=Persona.BULL, vote=vote, confidence=round(min(1.0, conf), 3),
        reasons=reasons or ["no strong positive signals; default APPROVE"],
        evidence=evidence,
    )


def bear_argue(
    *, stress: float, sizing_mult: float, regime: str, suggestion: str,
    dd_pct: float = 0.0,
) -> Argument:
    """Pessimist: "capital preservation first. What can go wrong?"."""
    reasons: list[str] = []
    if stress > 0.5:
        reasons.append(f"stress elevated ({stress:.0%}) -- tight risk")
    if sizing_mult < 0.5:
        reasons.append(f"sizing cut to {sizing_mult:.0%} -- market not supportive")
    if regime.upper() in {"CRISIS", "RISK_OFF", "RISK-OFF"}:
        reasons.append(f"regime={regime} -- defensive bias")
    if dd_pct > 0.02:
        reasons.append(f"already drawn down {dd_pct:.1%} today")
    if suggestion in {"STAND_ASIDE", "KILL", "REDUCE"}:
        reasons.append(f"v2 suggestion={suggestion} -- already cautionary")
    conf = 0.4 + 0.5 * stress + 0.1 * (1 - sizing_mult) + 5 * dd_pct
    conf = min(1.0, conf)
    vote = "DENY" if conf > 0.7 else "CONDITIONAL"
    return Argument(
        persona=Persona.BEAR, vote=vote, confidence=round(conf, 3),
        reasons=reasons or ["no strong negative; default conditional cap"],
        evidence=["doctrine: CAPITAL_FIRST"],
    )


def skeptic_argue(
    *, stress: float, regime: str, regime_confidence: float,
    events_count: int = 0,
) -> Argument:
    """Devil's advocate on BOTH sides: "what's everyone missing?"."""
    reasons: list[str] = []
    if regime_confidence < 0.5:
        reasons.append(
            f"regime {regime} is only {regime_confidence:.0%} confident "
            "-- classifier may be wrong",
        )
    if events_count > 2:
        reasons.append(
            f"{events_count} macro events queued -- single-event reasoning insufficient",
        )
    if 0.3 < stress < 0.6:
        reasons.append(
            "stress in the ambiguous zone -- no clear signal either way",
        )
    # Skeptic defaults to DEFER (needs more information) unless evidence is thin
    if not reasons:
        return Argument(
            persona=Persona.SKEPTIC, vote="CONDITIONAL", confidence=0.4,
            reasons=["nothing obviously wrong, but not decisive either"],
        )
    conf = 0.6 + 0.1 * len(reasons)
    return Argument(
        persona=Persona.SKEPTIC, vote="DEFER", confidence=round(min(1.0, conf), 3),
        reasons=reasons,
        evidence=["doctrine: ADVERSARIAL_HONESTY"],
    )


def historian_argue(
    *, precedent_n: int, precedent_win_rate: float | None,
    precedent_mean_r: float | None, precedent_suggestion: str = "",
) -> Argument:
    """Pulls past data: "what does the evidence say?"."""
    reasons: list[str] = []
    evidence: list[str] = []
    if precedent_n == 0:
        return Argument(
            persona=Persona.HISTORIAN, vote="CONDITIONAL", confidence=0.3,
            reasons=["no precedent in this bucket -- walking blind"],
            evidence=[],
        )
    reasons.append(f"n={precedent_n} past decisions in this bucket")
    evidence.append(precedent_suggestion or "precedent available")
    if precedent_win_rate is not None:
        reasons.append(f"historical win rate {precedent_win_rate:.0%}")
    if precedent_mean_r is not None:
        reasons.append(f"historical mean realized R {precedent_mean_r:+.2f}")
    if precedent_mean_r is not None and precedent_mean_r > 0.3 \
            and (precedent_win_rate or 0) >= 0.5:
        vote = "APPROVE"
        conf = min(1.0, 0.6 + 0.3 * (precedent_win_rate or 0.5))
    elif precedent_mean_r is not None and precedent_mean_r < -0.3:
        vote = "DENY"
        conf = min(1.0, 0.7 + 0.1 * -precedent_mean_r)
    else:
        vote = "CONDITIONAL"
        conf = 0.5
    return Argument(
        persona=Persona.HISTORIAN, vote=vote, confidence=round(conf, 3),
        reasons=reasons, evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def hold_debate(
    arguments: list[Argument],
    weights: dict[Persona, float] | None = None,
    now: datetime | None = None,
) -> DebateVerdict:
    """Tally votes and produce the debate verdict."""
    now = now or datetime.now(UTC)
    w = weights or DEFAULT_WEIGHTS
    score: dict[str, float] = {
        "APPROVE": 0.0, "CONDITIONAL": 0.0, "DENY": 0.0, "DEFER": 0.0,
    }
    total_weight = 0.0
    for arg in arguments:
        pw = w.get(arg.persona, 1.0) * arg.confidence
        score[arg.vote] += pw
        total_weight += pw
    if total_weight == 0:
        return DebateVerdict(
            ts=now, final_vote="CONDITIONAL", score=score,
            majority_by=0.0, transcript=arguments,
            summary="no votes",
            consensus=False,
        )
    # Normalize
    for k in score:
        score[k] = round(score[k] / total_weight, 4)
    winner = max(score.items(), key=lambda kv: kv[1])
    # Margin: difference vs second-place
    sorted_scores = sorted(score.values(), reverse=True)
    margin = sorted_scores[0] - (sorted_scores[1] if len(sorted_scores) > 1 else 0.0)
    votes = [a.vote for a in arguments]
    consensus = votes.count(winner[0]) >= max(1, len(arguments) - 1)
    summary = (
        f"{winner[0]} wins with score {winner[1]:.2f} "
        f"(margin {margin:.2f}, consensus={'yes' if consensus else 'no'})"
    )
    return DebateVerdict(
        ts=now,
        final_vote=winner[0],
        score=score,
        majority_by=round(margin, 4),
        transcript=arguments,
        summary=summary,
        consensus=consensus,
    )


def full_debate(
    *,
    stress: float,
    sizing_mult: float,
    regime: str,
    regime_confidence: float,
    suggestion: str,
    dd_pct: float = 0.0,
    events_count: int = 0,
    precedent_n: int = 0,
    precedent_win_rate: float | None = None,
    precedent_mean_r: float | None = None,
    precedent_suggestion: str = "",
    now: datetime | None = None,
) -> DebateVerdict:
    """Run all four personas and aggregate."""
    args = [
        bull_argue(
            stress=stress, sizing_mult=sizing_mult,
            regime=regime, suggestion=suggestion,
        ),
        bear_argue(
            stress=stress, sizing_mult=sizing_mult, regime=regime,
            suggestion=suggestion, dd_pct=dd_pct,
        ),
        skeptic_argue(
            stress=stress, regime=regime,
            regime_confidence=regime_confidence, events_count=events_count,
        ),
        historian_argue(
            precedent_n=precedent_n,
            precedent_win_rate=precedent_win_rate,
            precedent_mean_r=precedent_mean_r,
            precedent_suggestion=precedent_suggestion,
        ),
    ]
    return hold_debate(args, now=now)
