"""Safe decision-journal heartbeat writer for DR/readiness checks.

This script proves the canonical decision journal is writable without
starting bots, contacting brokers, or mirroring to external services.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.obs.decision_journal import (  # noqa: E402
    Actor,
    DecisionJournal,
    JournalEvent,
    Outcome,
)
from eta_engine.scripts.workspace_roots import ETA_RUNTIME_DECISION_JOURNAL_PATH  # noqa: E402


def _display_path(path: Path) -> str:
    """Return a workspace-relative path when possible."""
    try:
        return path.relative_to(ROOT.parent).as_posix()
    except ValueError:
        return str(path)


def build_smoke_event(*, source: str = "decision_journal_smoke") -> JournalEvent:
    """Build the minimal heartbeat event for the decision journal."""
    return JournalEvent(
        actor=Actor.OPERATOR,
        intent="decision_journal_smoke",
        rationale="DR/readiness writability heartbeat only; no broker or bot action",
        gate_checks=["+canonical_runtime_path", "+local_append_only"],
        outcome=Outcome.NOTED,
        metadata={
            "source": source,
            "status": "green",
            "dry_run": True,
            "broker_network": False,
            "supabase_mirror": False,
        },
    )


def append_decision_journal_smoke(
    journal_path: Path = ETA_RUNTIME_DECISION_JOURNAL_PATH,
    *,
    source: str = "decision_journal_smoke",
) -> dict[str, Any]:
    """Append one smoke event to ``journal_path`` and return operator evidence."""
    journal = DecisionJournal(journal_path, supabase_mirror=False)
    event = journal.append(build_smoke_event(source=source))
    return {
        "path": _display_path(journal_path),
        "bytes": journal_path.stat().st_size,
        "record": event.model_dump(mode="json"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="decision_journal_smoke")
    parser.add_argument(
        "--journal-path",
        type=Path,
        default=ETA_RUNTIME_DECISION_JOURNAL_PATH,
        help=f"decision journal JSONL path (default: {ETA_RUNTIME_DECISION_JOURNAL_PATH})",
    )
    parser.add_argument("--source", default="decision_journal_smoke")
    parser.add_argument("--json", action="store_true", help="emit machine-readable evidence")
    args = parser.parse_args(argv)

    evidence = append_decision_journal_smoke(args.journal_path, source=args.source)
    if args.json:
        print(json.dumps(evidence, indent=2, sort_keys=True))
    else:
        print(
            "[decision_journal_smoke] appended decision_journal_smoke to "
            f"{evidence['path']} ({evidence['bytes']} bytes)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
