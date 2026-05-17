"""Daily kaizen close-cycle.

Lever 1 (2026-04-26): activates the kaizen scaffolding that's been built
in ``eta_engine/brain/jarvis_v3/kaizen.py`` but wasn't wired to a scheduled
task. Run by ``Eta-Kaizen-DailyClose`` at 22:30 ET each day.

What it does
------------
  1. Loads the day's decision journal events (default: last 24h, configurable)
  2. Synthesizes ``went_well`` / ``went_poorly`` / ``surprises`` from
     event Outcomes
  3. Computes KPIs (per-Outcome counts, override rate)
  4. Calls ``kaizen.close_cycle(...)`` to produce the Retrospective + the
     mandated +1 ticket
  5. Persists both to the canonical kaizen ledger JSON
  6. Fires a Resend ``kaizen_plus_one`` alert with the +1 ticket title

Doctrine: every cycle MUST emit at least one +1 ticket -- Kaizen = +1 always.
This script is the mechanism that enforces that doctrine.

Usage
-----
  python -m eta_engine.scripts.run_kaizen_close_cycle
  python -m eta_engine.scripts.run_kaizen_close_cycle --window-hours 168 --cycle WEEKLY
  python -m eta_engine.scripts.run_kaizen_close_cycle --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Make the package importable when run as a script (not -m).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.brain.jarvis_v3.kaizen import (  # noqa: E402
    CycleKind,
    KaizenLedger,
    KaizenTicket,
    Retrospective,
    close_cycle,
)
from eta_engine.obs.decision_journal import (  # noqa: E402
    DecisionJournal,
    JournalEvent,
    Outcome,
    default_journal,
)
from eta_engine.scripts import workspace_roots  # noqa: E402

logger = logging.getLogger("kaizen_close_cycle")


def synthesize_inputs(events: list[JournalEvent]) -> dict[str, Any]:
    """Turn raw journal events into kaizen close_cycle() inputs.

    Heuristic:
      * went_well: top 3 most common intents whose outcome was NOT
        BLOCKED / FAILED / OVERRIDDEN
      * went_poorly: top 3 most common intents that DID block, fail, or
        get overridden
      * surprises: intents that appear ONCE with a non-NOTED outcome
        (low base rate -> potentially noteworthy)
      * kpis: per-Outcome counts + override rate
    """
    went_well_pool: Counter[str] = Counter()
    went_poorly_pool: Counter[str] = Counter()
    once_seen: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()
    bad_outcomes = {Outcome.BLOCKED, Outcome.FAILED, Outcome.OVERRIDDEN}

    for ev in events:
        outcome_counts[ev.outcome.value] += 1
        if ev.outcome in bad_outcomes:
            went_poorly_pool[ev.intent] += 1
        else:
            went_well_pool[ev.intent] += 1
        once_seen[ev.intent] += 1

    surprises = [
        intent
        for intent, n in once_seen.items()
        if n == 1 and any(ev.intent == intent and ev.outcome != Outcome.NOTED for ev in events)
    ]

    kpis: dict[str, float] = {f"outcome_{k.lower()}": float(v) for k, v in outcome_counts.items()}
    total = sum(outcome_counts.values())
    override_n = outcome_counts.get(Outcome.OVERRIDDEN.value, 0)
    kpis["override_rate"] = (override_n / total) if total else 0.0
    kpis["total_events"] = float(total)

    # Tier-2 #7 (2026-04-27): outcome -> realized P&L feedback when present.
    # If events have metadata['realized_r'] (R-multiple of the resulting
    # trade), aggregate it so the kaizen synthesizer sees money outcomes
    # not just gate firings. This lets `went_poorly` capture losing
    # trades, not just blocked actions.
    realized_rs: list[float] = []
    losing_intents: Counter[str] = Counter()
    winning_intents: Counter[str] = Counter()
    for ev in events:
        r = ev.metadata.get("realized_r") if isinstance(ev.metadata, dict) else None
        if isinstance(r, (int, float)):
            realized_rs.append(float(r))
            if r < 0:
                losing_intents[ev.intent] += 1
            elif r > 0:
                winning_intents[ev.intent] += 1
    if realized_rs:
        kpis["realized_r_total"] = round(sum(realized_rs), 4)
        kpis["realized_r_mean"] = round(sum(realized_rs) / len(realized_rs), 4)
        kpis["winning_count"] = float(sum(1 for r in realized_rs if r > 0))
        kpis["losing_count"] = float(sum(1 for r in realized_rs if r < 0))

    # If we have P&L data, ground "went_well/poorly" in actual money outcomes
    if winning_intents or losing_intents:
        went_well_lines = [f"{intent} +R (×{n})" for intent, n in winning_intents.most_common(3)]
        went_poorly_lines = [f"{intent} -R (×{n})" for intent, n in losing_intents.most_common(3)]
    else:
        went_well_lines = [f"{intent} (×{n})" for intent, n in went_well_pool.most_common(3)]
        went_poorly_lines = [f"{intent} (×{n})" for intent, n in went_poorly_pool.most_common(3)]

    return {
        "went_well": went_well_lines,
        "went_poorly": went_poorly_lines,
        "surprises": surprises[:5],
        "kpis": kpis,
    }


def fire_alert(ticket: KaizenTicket, retro: Retrospective, *, alerts_yaml: Path | None = None) -> None:
    """Best-effort Resend alert with the +1 ticket title."""
    try:
        import yaml
        from eta_engine.obs.alert_dispatcher import AlertDispatcher

        cfg_path = alerts_yaml or (ROOT / "configs" / "alerts.yaml")
        if not cfg_path.exists():
            logger.warning("alerts config not found at %s; skipping notification", cfg_path)
            return
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        dispatcher = AlertDispatcher(cfg)
        result = dispatcher.send(
            "kaizen_plus_one",
            {
                "ticket_id": ticket.id,
                "title": ticket.title,
                "rationale": ticket.rationale,
                "impact": ticket.impact,
                "cycle": retro.cycle_kind.value,
                "window_start": retro.window_start.isoformat(),
                "window_end": retro.window_end.isoformat(),
                "kpis": retro.kpis,
                "lessons": retro.lessons,
            },
        )
        logger.info(
            "alert dispatched: %s -> delivered=%s blocked=%s", "kaizen_plus_one", result.delivered, result.blocked
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert dispatch failed (non-fatal): %s", exc)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--journal",
        type=Path,
        default=None,
        help="Decision journal JSONL (defaults to var/eta_engine/state/decision_journal.jsonl)",
    )
    p.add_argument(
        "--ledger",
        type=Path,
        default=workspace_roots.ETA_KAIZEN_LEDGER_PATH,
        help="Kaizen ledger JSON (default: var/eta_engine/state/kaizen_ledger.json)",
    )
    p.add_argument(
        "--window-hours", type=float, default=24.0, help="Look back this many hours for events (default: 24)"
    )
    p.add_argument(
        "--cycle", type=str, default="DAILY", choices=[k.value for k in CycleKind], help="Cycle kind (default: DAILY)"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Synthesize + print but don't append to ledger or fire alerts"
    )
    p.add_argument("--no-alert", action="store_true", help="Skip the Resend notification even on a real run")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Load journal events in window
    journal = DecisionJournal(args.journal) if args.journal else default_journal()
    window_end = datetime.now(UTC)
    window_start = window_end - timedelta(hours=args.window_hours)
    events = journal.read_since(window_start)
    logger.info("loaded %d events from %s (window: last %.1f hours)", len(events), journal.path, args.window_hours)

    if not events:
        logger.warning(
            "no events in window -- emitting baseline +1 anyway (Kaizen doctrine: every cycle MUST produce a ticket)"
        )

    inputs = synthesize_inputs(events)
    logger.info(
        "synthesized: well=%d went_poorly=%d surprises=%d total_events=%.0f",
        len(inputs["went_well"]),
        len(inputs["went_poorly"]),
        len(inputs["surprises"]),
        inputs["kpis"].get("total_events", 0),
    )

    retro, ticket = close_cycle(
        cycle_kind=CycleKind(args.cycle),
        window_start=window_start,
        window_end=window_end,
        went_well=inputs["went_well"],
        went_poorly=inputs["went_poorly"],
        surprises=inputs["surprises"],
        kpis=inputs["kpis"],
        now=window_end,
    )

    print()
    print(f"  ticket: {ticket.id}")
    print(f"  title:  {ticket.title}")
    print(f"  impact: {ticket.impact}")
    print("  rationale:")
    print(f"    {ticket.rationale}")
    print()

    if args.dry_run:
        print("  (dry-run) ledger NOT appended; alert NOT fired")
        return 0

    # KaizenLedger persists via load() + add_retro/add_ticket + save() roundtrip.
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger = KaizenLedger.load(args.ledger)
    ledger.add_retro(retro)
    ledger.add_ticket(ticket)
    ledger.save(args.ledger)
    logger.info(
        "appended retro + ticket to %s (now: %d retros, %d tickets)",
        args.ledger,
        len(ledger.retrospectives()),
        len(ledger.tickets()),
    )

    if not args.no_alert:
        fire_alert(ticket, retro)

    return 0


if __name__ == "__main__":
    sys.exit(main())
