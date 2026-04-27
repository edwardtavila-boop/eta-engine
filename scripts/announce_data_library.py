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
        [--journal docs/decision_journal.jsonl]
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
        "--journal", type=Path, default=ROOT / "docs" / "decision_journal.jsonl",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    lib = default_library()
    print(lib.summary_markdown())
    print()

    if args.dry_run:
        print("(dry-run: no JARVIS event written)")
        return 0

    payload = lib.summary_jarvis_payload()
    journal = DecisionJournal(args.journal)
    journal.append(
        JournalEvent(
            actor=Actor.JARVIS,
            intent="data_inventory",
            rationale=(
                f"library refreshed: {len(payload)} datasets across "
                f"{len(lib.symbols())} symbols, {len(lib.timeframes())} timeframes"
            ),
            gate_checks=[
                f"+datasets:{len(payload)}",
                f"+symbols:{len(lib.symbols())}",
            ],
            outcome=Outcome.NOTED,
            metadata={"datasets": payload, "roots": [str(r) for r in lib.roots]},
        )
    )
    print(f"[announce_data_library] JARVIS event appended to {args.journal}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
