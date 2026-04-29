"""
EVOLUTIONARY TRADING ALGO  //  scripts.announce_data_library
=============================================================
Emit the current ``data.library`` inventory as a single
``Actor.JARVIS`` event on the decision journal so JARVIS (and any
operator scanning the journal) knows what's testable without
walking the filesystem.

Designed to be re-run after data fetch jobs complete — the latest
JARVIS event with ``intent="data_inventory"`` is the canonical
"what's available right now" snapshot.

Usage::

    python -m eta_engine.scripts.announce_data_library
        [--journal var/eta_engine/state/decision_journal.jsonl]
        [--dry-run]

The dry-run flag prints the markdown summary but doesn't append the
event — useful for operator-side eyeballing before publishing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import ETA_RUNTIME_DECISION_JOURNAL_PATH  # noqa: E402

_DEFAULT_JOURNAL = ETA_RUNTIME_DECISION_JOURNAL_PATH


def main() -> int:
    from eta_engine.data.library import default_library
    from eta_engine.obs.decision_journal import (
        Actor,
        DecisionJournal,
        JournalEvent,
        Outcome,
    )

    p = argparse.ArgumentParser(prog="announce_data_library")
    p.add_argument(
        "--journal",
        type=Path,
        default=_DEFAULT_JOURNAL,
        help="Decision journal JSONL (default: var/eta_engine/state/decision_journal.jsonl)",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    lib = default_library()
    print(lib.summary_markdown())
    print()

    # Bot-coverage audit. Surfaces which bots can run vs which are
    # blocked on missing data feeds (especially crypto).
    from eta_engine.data.audit import audit_all
    from eta_engine.data.audit import summary_markdown as audit_summary
    audits = audit_all(library=lib)
    print(audit_summary(audits))
    print()

    if args.dry_run:
        print("(dry-run: no JARVIS event written)")
        return 0

    payload = lib.summary_jarvis_payload()
    runnable = [a.bot_id for a in audits if a.is_runnable]
    blocked = {
        a.bot_id: {
            "missing_critical": [
                {"kind": r.kind, "symbol": r.symbol, "timeframe": r.timeframe}
                for r in a.missing_critical
            ],
            "sources_hint": list(a.sources_hint),
        }
        for a in audits if not a.is_runnable
    }

    journal = DecisionJournal(args.journal)
    journal.append(
        JournalEvent(
            actor=Actor.JARVIS,
            intent="data_inventory",
            rationale=(
                f"library refreshed: {len(payload)} datasets, "
                f"{len(lib.symbols())} symbols, {len(lib.timeframes())} timeframes; "
                f"{len(runnable)}/{len(audits)} bots runnable, "
                f"{len(blocked)} blocked on missing critical feeds"
            ),
            gate_checks=[
                f"+datasets:{len(payload)}",
                f"+runnable_bots:{len(runnable)}",
                f"-blocked_bots:{len(blocked)}",
            ],
            outcome=Outcome.NOTED if not blocked else Outcome.BLOCKED,
            metadata={
                "datasets": payload,
                "roots": [str(r) for r in lib.roots],
                "runnable_bots": runnable,
                "blocked_bots": blocked,
            },
        )
    )
    print(f"[announce_data_library] JARVIS event appended to {args.journal}")
    return 0 if not blocked else 1


if __name__ == "__main__":
    sys.exit(main())
