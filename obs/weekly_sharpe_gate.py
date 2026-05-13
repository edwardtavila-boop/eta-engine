"""
EVOLUTIONARY TRADING ALGO  //  obs.weekly_sharpe_gate
=====================================================
Weekly OOS Sharpe gate — devils-advocate's 2026-04-27 "kill at <0"
recommendation. Operator-facing: emits a journal GRADER event for
each bot whose recent realized-Sharpe is below threshold, but does
NOT auto-demote. The operator decides whether to flip
``extras["deactivated"]=True`` based on the assessment.

Why human-in-the-loop
---------------------
Auto-demotion on a single weekly check creates a hair-trigger that
can fire on a small unlucky sample (10 trades over a week is
common for ORB-style strategies; one bad day swings Sharpe wildly).
Devils-advocate's spec was "kill at <0" — we surface the verdict
so the operator can sanity-check the trades, not flip the bot off
behind their back.

Math
----
Reads recent N trades for a bot via the existing
``trades_from_journal`` helper, takes the per-trade R-multiple as
the return series, and applies the canonical
``backtest.metrics.compute_sharpe``. The constant-returns guard
already lives in compute_sharpe — degenerate samples (every fill
hit the same R-target) return Sharpe=0.0, which is below
threshold and surfaces a green/amber assessment without a misleading
"infinite Sharpe" alert.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from eta_engine.backtest.metrics import compute_sharpe
from eta_engine.obs.decision_journal import (
    Actor,
    DecisionJournal,
    JournalEvent,
    Outcome,
)
from eta_engine.obs.drift_watchdog import trades_from_journal
from eta_engine.strategies.per_bot_registry import (
    all_assignments,
    is_active,
)

Severity = Literal["green", "amber", "red"]


class SharpeGateAssessment(BaseModel):
    """Per-bot weekly Sharpe verdict.

    ``recommended_action`` is the operator-facing instruction:
      * ``"continue"`` -- Sharpe at/above threshold; no action.
      * ``"review"`` -- Sharpe below threshold but sample still
        small or borderline; operator should look at the trades.
      * ``"demote"`` -- Sharpe materially below threshold over a
        meaningful sample; operator should flip
        ``extras["deactivated"]=True`` until the strategy is
        diagnosed.
    """

    bot_id: str
    strategy_id: str
    severity: Severity
    n_trades: int = Field(ge=0)
    sharpe: float
    threshold: float
    recommended_action: Literal["continue", "review", "demote"]
    reasons: list[str] = Field(default_factory=list)


def assess_bot_sharpe(
    *,
    journal: DecisionJournal,
    bot_id: str,
    strategy_id: str,
    last_n: int = 30,
    min_trades: int = 10,
    threshold: float = 0.0,
    review_band: float = 0.5,
) -> SharpeGateAssessment:
    """Compute the recent realized Sharpe for one bot and tier the verdict.

    ``last_n`` -- how many recent trades to consider (default 30).
    ``min_trades`` -- minimum sample below which we emit ``green``
        with an "insufficient sample" reason (don't flap on the
        first few fills).
    ``threshold`` -- the kill line; anything strictly below counts
        as a violation.
    ``review_band`` -- range above the threshold where we surface
        ``amber`` instead of ``green`` (Sharpe in
        ``[threshold, threshold + review_band]`` -> "review"). This
        gives the operator early warning before the bot is below
        the kill line.
    """
    trades = trades_from_journal(journal, strategy_id=strategy_id, last_n=last_n)
    n = len(trades)
    if n < min_trades:
        return SharpeGateAssessment(
            bot_id=bot_id,
            strategy_id=strategy_id,
            severity="green",
            n_trades=n,
            sharpe=0.0,
            threshold=threshold,
            recommended_action="continue",
            reasons=[f"insufficient sample: {n} < {min_trades} trades"],
        )

    rs = [float(t.pnl_r) for t in trades]
    sharpe = compute_sharpe(rs)

    if sharpe < threshold:
        return SharpeGateAssessment(
            bot_id=bot_id,
            strategy_id=strategy_id,
            severity="red",
            n_trades=n,
            sharpe=sharpe,
            threshold=threshold,
            recommended_action="demote",
            reasons=[
                f"realized Sharpe {sharpe:+.2f} below kill threshold "
                f"{threshold:+.2f} over {n} trades; operator should "
                "flip extras['deactivated']=True until diagnosed."
            ],
        )
    if sharpe < threshold + review_band:
        return SharpeGateAssessment(
            bot_id=bot_id,
            strategy_id=strategy_id,
            severity="amber",
            n_trades=n,
            sharpe=sharpe,
            threshold=threshold,
            recommended_action="review",
            reasons=[
                f"realized Sharpe {sharpe:+.2f} in review band "
                f"[{threshold:+.2f},{threshold + review_band:+.2f}] "
                f"over {n} trades; eyeball the recent trades."
            ],
        )
    return SharpeGateAssessment(
        bot_id=bot_id,
        strategy_id=strategy_id,
        severity="green",
        n_trades=n,
        sharpe=sharpe,
        threshold=threshold,
        recommended_action="continue",
        reasons=[f"realized Sharpe {sharpe:+.2f} >= {threshold + review_band:+.2f} over {n} trades"],
    )


def run_once(
    *,
    journal: DecisionJournal,
    last_n: int = 30,
    min_trades: int = 10,
    threshold: float = 0.0,
    review_band: float = 0.5,
    write_event: bool = True,
    skip_deactivated: bool = True,
) -> list[SharpeGateAssessment]:
    """Walk the registry, assess every active bot's recent Sharpe,
    optionally emit a GRADER event per bot.

    ``skip_deactivated`` (default True) avoids re-assessing bots
    that are already muted via ``extras["deactivated"]=True`` — no
    point flagging an already-disabled bot. Pass False for a full
    fleet audit including muted bots.

    Returns the list of assessments in registry order.
    """
    out: list[SharpeGateAssessment] = []
    for a in all_assignments():
        if skip_deactivated and not is_active(a):
            continue
        assessment = assess_bot_sharpe(
            journal=journal,
            bot_id=a.bot_id,
            strategy_id=a.strategy_id,
            last_n=last_n,
            min_trades=min_trades,
            threshold=threshold,
            review_band=review_band,
        )
        out.append(assessment)
        if write_event:
            outcome = Outcome.NOTED if assessment.severity == "green" else Outcome.BLOCKED
            rationale = "; ".join(assessment.reasons) or "no flag"
            journal.append(
                JournalEvent(
                    actor=Actor.GRADER,
                    intent=f"weekly_sharpe:{a.bot_id}",
                    rationale=rationale,
                    gate_checks=[
                        f"+severity:{assessment.severity}",
                        f"+sharpe:{assessment.sharpe:+.3f}",
                        f"+threshold:{assessment.threshold:+.3f}",
                        f"+n_trades:{assessment.n_trades}",
                        f"+action:{assessment.recommended_action}",
                    ],
                    outcome=outcome,
                    links=[f"bot:{a.bot_id}", f"strategy:{a.strategy_id}"],
                    metadata={
                        "bot_id": a.bot_id,
                        "strategy_id": a.strategy_id,
                        "severity": assessment.severity,
                        "sharpe": round(assessment.sharpe, 4),
                        "threshold": assessment.threshold,
                        "n_trades": assessment.n_trades,
                        "recommended_action": assessment.recommended_action,
                    },
                )
            )
    return out
