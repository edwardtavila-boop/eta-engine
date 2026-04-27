"""
EVOLUTIONARY TRADING ALGO  //  obs.fleet_correlation_watchdog
=============================================================
Fleet-correlation runner — periodic glue between
``per_bot_registry`` (declares which bot pairs to watch via
``extras["fleet_corr_partner"]``) and ``obs.drift_monitor.
assess_fleet_correlation`` (the math).

Why this exists
---------------
Quant-sage flagged on 2026-04-27 that BTC + ETH on the same
``crypto_orb`` strategy may be one strategy not two — the registry
already tags the pair via ``extras["fleet_corr_partner"]`` but
nothing reads it yet. This module is the reader: it walks every
registered bot, finds its declared partner, loads recent trades for
both, and emits a ``FleetCorrelationAssessment`` to the
decision_journal as a ``GRADER`` event so the dashboard / alerts
layer can surface the verdict.

The watchdog is intentionally **read-only**. It does NOT change
risk budgets or pause bots — that's the operator's call based on
the assessment. The journal event is the canonical surface; what
acts on it lives in ``brain/avengers``.

Adoption
--------
* JARVIS daemon's tick (low-frequency, e.g. every 30 minutes)
* Standalone cron / scheduled-task wrapper
* Operator one-shot (``python -m eta_engine.scripts.fleet_corr_check``)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eta_engine.obs.decision_journal import (
    Actor,
    DecisionJournal,
    JournalEvent,
    Outcome,
)
from eta_engine.obs.drift_monitor import (
    FleetCorrelationAssessment,
    assess_fleet_correlation,
)
from eta_engine.obs.drift_watchdog import trades_from_journal
from eta_engine.strategies.per_bot_registry import (
    StrategyAssignment,
    all_assignments,
    get_for_bot,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


def _partner_pairs(
    assignments: Iterable[StrategyAssignment] | None = None,
) -> list[tuple[str, str]]:
    """Enumerate every (bot_a, partner) pair declared via
    ``extras["fleet_corr_partner"]``, deduplicated so each pair
    surfaces once regardless of which side declared the link.
    """
    if assignments is None:
        assignments = all_assignments()
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for a in assignments:
        partner = a.extras.get("fleet_corr_partner")
        if not isinstance(partner, str) or not partner or partner == a.bot_id:
            continue
        # Verify the partner is actually registered — silently drop
        # dangling references rather than blowing up the daemon.
        if get_for_bot(partner) is None:
            continue
        key = tuple(sorted((a.bot_id, partner)))
        if key in seen:
            continue
        seen.add(key)
        out.append((a.bot_id, partner))
    return out


def assess_pair(
    *,
    journal: DecisionJournal,
    bot_a: str,
    bot_b: str,
    last_n: int = 50,
    min_paired: int = 10,
    amber_rho: float = 0.5,
    red_rho: float = 0.7,
) -> FleetCorrelationAssessment:
    """Load recent trades for both bots and run the correlation check.

    Returns the assessment without writing to the journal so callers
    can decide whether the result is worth surfacing.
    """
    # Trade lookup is keyed by strategy_id, not bot_id — derive it
    # from the registry. If the bot has no assignment (shouldn't
    # happen, but defensive), use bot_id verbatim as the lookup key.
    a = get_for_bot(bot_a)
    b = get_for_bot(bot_b)
    sid_a = a.strategy_id if a is not None else bot_a
    sid_b = b.strategy_id if b is not None else bot_b
    trades_a = trades_from_journal(journal, strategy_id=sid_a, last_n=last_n)
    trades_b = trades_from_journal(journal, strategy_id=sid_b, last_n=last_n)
    return assess_fleet_correlation(
        bot_a=bot_a, recent_a=trades_a,
        bot_b=bot_b, recent_b=trades_b,
        min_paired=min_paired,
        amber_rho=amber_rho,
        red_rho=red_rho,
    )


def run_once(
    *,
    journal: DecisionJournal,
    last_n: int = 50,
    min_paired: int = 10,
    amber_rho: float = 0.5,
    red_rho: float = 0.7,
    write_event: bool = True,
) -> list[FleetCorrelationAssessment]:
    """Walk the registry, assess every fleet_corr_partner pair, optionally
    emit a journal event for each.

    Returns the list of assessments in registry order so callers can
    act on green results (e.g. clear a previous amber flag).
    """
    out: list[FleetCorrelationAssessment] = []
    for bot_a, bot_b in _partner_pairs():
        assessment = assess_pair(
            journal=journal,
            bot_a=bot_a, bot_b=bot_b,
            last_n=last_n, min_paired=min_paired,
            amber_rho=amber_rho, red_rho=red_rho,
        )
        out.append(assessment)
        if write_event:
            outcome = (
                Outcome.NOTED if assessment.severity == "green"
                else Outcome.BLOCKED
            )
            rationale = "; ".join(assessment.reasons) or "no correlation flag"
            journal.append(
                JournalEvent(
                    actor=Actor.GRADER,
                    intent=f"fleet_corr:{bot_a}+{bot_b}",
                    rationale=rationale,
                    gate_checks=[
                        f"+severity:{assessment.severity}",
                        f"+rho:{assessment.rho:+.3f}",
                        f"+n_paired:{assessment.n_paired}",
                        f"+action:{assessment.recommended_action}",
                    ],
                    outcome=outcome,
                    links=[f"bot:{bot_a}", f"bot:{bot_b}"],
                    metadata={
                        "bot_a": bot_a,
                        "bot_b": bot_b,
                        "severity": assessment.severity,
                        "rho": round(assessment.rho, 4),
                        "n_paired": assessment.n_paired,
                        "recommended_action": assessment.recommended_action,
                    },
                )
            )
    return out
