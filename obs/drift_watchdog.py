"""
EVOLUTIONARY TRADING ALGO  //  obs.drift_watchdog
==========================================
Drift-monitor adoption layer: load recent trades from decision_journal,
run ``assess_drift``, emit the result back to the journal as a
``GRADER`` event.

This is the glue between ``obs.drift_monitor`` (pure compute) and
``obs.decision_journal`` (ledger). Designed to be called from:

* JARVIS daemon's tick (low-frequency, e.g. every 5 minutes)
* A standalone cron / scheduled-task wrapper (``scripts/drift_check.py``)
* An ad-hoc operator command (``python -m eta_engine.scripts.drift_check``)

Why it lives in ``obs/`` and not ``brain/avengers/``
----------------------------------------------------
The Avengers daemon owns *task scheduling*. This module owns
*computing the drift signal + writing the event*. The daemon imports
this and invokes ``run_once`` on its tick — that keeps the daemon
file thin and the watchdog testable in isolation without simulating
the whole avenger envelope.

Trade reconstruction
--------------------
``decision_journal`` events of type ``Actor.TRADE_ENGINE`` with
``Outcome.EXECUTED`` are the canonical "a trade happened" rows. The
watchdog reconstructs a ``Trade`` from each event's metadata. Events
that don't carry the full Trade payload (legacy rows pre-2026-04
schema, or partially-populated heartbeats) are silently skipped —
the drift assessment is based on whatever full rows are available.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from eta_engine.backtest.models import Trade
from eta_engine.obs.decision_journal import (
    Actor,
    DecisionJournal,
    JournalEvent,
    Outcome,
)
from eta_engine.obs.drift_monitor import (
    BaselineSnapshot,
    DriftAssessment,
    assess_drift,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def trades_from_journal(
    journal: DecisionJournal,
    *,
    strategy_id: str,
    last_n: int = 50,
) -> list[Trade]:
    """Reconstruct the most recent N executed trades for a strategy.

    Filters the journal to ``Actor.TRADE_ENGINE`` + ``Outcome.EXECUTED``
    events whose metadata contains a ``strategy_id`` matching the
    requested strategy. Tries to instantiate a Trade from each event's
    metadata; rows that don't validate are skipped (logged as debug
    via pydantic's ValidationError, not raised).

    Returns trades in chronological order (oldest first) so caller can
    take the tail.
    """
    out: list[Trade] = []
    for event in journal.iter_all():
        if event.actor != Actor.TRADE_ENGINE or event.outcome != Outcome.EXECUTED:
            continue
        meta = event.metadata or {}
        if meta.get("strategy_id") != strategy_id:
            continue
        # The canonical layout: metadata["trade"] is the Trade
        # model_dump'd. Some legacy rows store fields at the top
        # level — try both.
        payload = meta.get("trade") if "trade" in meta else meta
        try:
            trade = Trade.model_validate(payload)
        except (ValidationError, TypeError):
            continue
        out.append(trade)
    return out[-last_n:]


def run_once(
    *,
    journal: DecisionJournal,
    strategy_id: str,
    baseline: BaselineSnapshot,
    last_n: int = 50,
    min_trades: int = 20,
    amber_z: float = 2.0,
    red_z: float = 3.0,
    write_event: bool = True,
) -> DriftAssessment:
    """Load recent trades, assess drift, optionally emit GRADER event.

    Returns the assessment regardless of severity so callers can act
    on green results too (e.g. to clear a previous amber flag).

    ``write_event=False`` is useful for unit tests and dry-run
    operator commands. When True, the assessment is appended to the
    journal as an ``Actor.GRADER`` event with severity in metadata
    so downstream readers (dashboards, alerts) can filter.
    """
    recent = trades_from_journal(journal, strategy_id=strategy_id, last_n=last_n)
    assessment = assess_drift(
        strategy_id=strategy_id,
        recent=recent,
        baseline=baseline,
        min_trades=min_trades,
        amber_z=amber_z,
        red_z=red_z,
    )

    if write_event:
        # Outcome maps from severity: green=NOTED (info), amber/red=BLOCKED
        # (worth surfacing). The "BLOCKED" naming is a stretch for a soft
        # warning, but the journal's Outcome enum doesn't have an "AMBER"
        # tier and adding one here would be a bigger refactor.
        outcome = Outcome.NOTED if assessment.severity == "green" else Outcome.BLOCKED
        rationale = "; ".join(assessment.reasons) or "no drift"
        journal.append(
            JournalEvent(
                actor=Actor.GRADER,
                intent=f"drift_check:{strategy_id}",
                rationale=rationale,
                gate_checks=[
                    f"+severity:{assessment.severity}",
                    f"+n_recent:{assessment.n_recent}",
                ],
                outcome=outcome,
                links=[f"strategy:{strategy_id}"],
                metadata={
                    "severity": assessment.severity,
                    "win_rate_z": round(assessment.win_rate_z, 4),
                    "avg_r_z": round(assessment.avg_r_z, 4),
                    "recent_win_rate": round(assessment.recent_win_rate, 4),
                    "recent_avg_r": round(assessment.recent_avg_r, 4),
                    "baseline_win_rate": round(baseline.win_rate, 4),
                    "baseline_avg_r": round(baseline.avg_r, 4),
                },
            )
        )

    return assessment


def run_all(
    *,
    journal: DecisionJournal,
    strategy_baselines: "Sequence[tuple[str, BaselineSnapshot]]",
    **kwargs: object,
) -> dict[str, DriftAssessment]:
    """Convenience: run drift check across a portfolio of strategies.

    ``strategy_baselines`` is a sequence of ``(strategy_id, baseline)``
    pairs. Returns a dict keyed by strategy_id. Useful from a daemon
    tick that monitors every promoted strategy on every cycle.
    """
    return {
        sid: run_once(journal=journal, strategy_id=sid, baseline=bl, **kwargs)  # type: ignore[arg-type]
        for sid, bl in strategy_baselines
    }
