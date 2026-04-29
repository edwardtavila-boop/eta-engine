"""
EVOLUTIONARY TRADING ALGO  //  scripts.drift_check
====================================================
CLI wrapper around ``obs.drift_watchdog.run_once`` so an operator
(or a Windows scheduled task / cron) can drift-check a strategy
without writing Python.

Usage::

    python -m eta_engine.scripts.drift_check \\
        --strategy mnq_v3 \\
        --journal var/eta_engine/state/decision_journal.jsonl \\
        --baseline-trades 200 \\
        --baseline-win-rate 0.6 \\
        --baseline-avg-r 0.4 \\
        --baseline-r-stddev 1.0 \\
        [--last-n 50] [--min-trades 20] \\
        [--amber-z 2.0] [--red-z 3.0] [--dry-run]

Exit code mirrors the assessment severity:
    0 = green
    1 = amber
    2 = red

That makes this script directly composable in shell pipelines /
scheduled tasks: any non-zero exit is a flag worth pinging the
operator on.

Real deployment flow
--------------------
The baseline is a property of the *strategy* (what the framework
saw at promotion time), not the watchdog. Best practice: store the
baseline as a JSON file alongside the strategy, point at it from
the cron command. A future iteration will support
``--baseline-file path.json`` to load it without 4 separate flags.
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
    from eta_engine.obs.decision_journal import DecisionJournal
    from eta_engine.obs.drift_monitor import BaselineSnapshot
    from eta_engine.obs.drift_watchdog import run_once

    p = argparse.ArgumentParser(
        prog="drift_check",
        description="Compute drift severity for a strategy and write a GRADER event back to the journal.",
    )
    p.add_argument("--strategy", required=True, help="strategy_id to monitor")
    p.add_argument(
        "--journal",
        type=Path,
        default=_DEFAULT_JOURNAL,
        help="path to decision_journal.jsonl (default: var/eta_engine/state/decision_journal.jsonl)",
    )
    p.add_argument("--baseline-trades", type=int, required=True)
    p.add_argument("--baseline-win-rate", type=float, required=True)
    p.add_argument("--baseline-avg-r", type=float, required=True)
    p.add_argument("--baseline-r-stddev", type=float, required=True)
    p.add_argument("--last-n", type=int, default=50, help="recent trades to load (default 50)")
    p.add_argument("--min-trades", type=int, default=20, help="min sample for assessment (default 20)")
    p.add_argument("--amber-z", type=float, default=2.0)
    p.add_argument("--red-z", type=float, default=3.0)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print assessment but do NOT append a GRADER event",
    )
    args = p.parse_args()

    baseline = BaselineSnapshot(
        strategy_id=args.strategy,
        n_trades=args.baseline_trades,
        win_rate=args.baseline_win_rate,
        avg_r=args.baseline_avg_r,
        r_stddev=args.baseline_r_stddev,
    )
    journal = DecisionJournal(args.journal)

    assessment = run_once(
        journal=journal,
        strategy_id=args.strategy,
        baseline=baseline,
        last_n=args.last_n,
        min_trades=args.min_trades,
        amber_z=args.amber_z,
        red_z=args.red_z,
        write_event=not args.dry_run,
    )

    print(f"strategy:        {assessment.strategy_id}")
    print(f"severity:        {assessment.severity.upper()}")
    print(f"n_recent:        {assessment.n_recent}")
    print(f"recent_win_rate: {assessment.recent_win_rate * 100:.2f}%")
    print(f"recent_avg_r:    {assessment.recent_avg_r:+.4f}")
    print(f"win_rate_z:      {assessment.win_rate_z:+.3f}")
    print(f"avg_r_z:         {assessment.avg_r_z:+.3f}")
    if assessment.reasons:
        print("reasons:")
        for r in assessment.reasons:
            print(f"  - {r}")
    if args.dry_run:
        print("(dry-run: no GRADER event written)")

    return {"green": 0, "amber": 1, "red": 2}[assessment.severity]


if __name__ == "__main__":
    sys.exit(main())
