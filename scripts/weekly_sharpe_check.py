"""
EVOLUTIONARY TRADING ALGO  //  scripts.weekly_sharpe_check
==========================================================
Weekly OOS Sharpe gate runner — devils-advocate's 2026-04-27
"kill at <0" recommendation. Walks every active bot in the
registry, computes recent realized Sharpe from journal trades,
emits a GRADER event per bot.

Designed for a Windows scheduled task / cron. Recommend Sunday
night so the operator wakes Monday with the verdict and decides
whether to demote any flagged bots before the week starts.

    schtasks /Create /SC WEEKLY /D SUN /ST 23:00 /TN ETA-WeeklySharpe /TR \\
        "python -m eta_engine.scripts.weekly_sharpe_check"

Exit code:
  * 0 = every bot is GREEN (above threshold + review band)
  * 1 = at least one bot is AMBER (in review band, just above kill)
  * 2 = at least one bot is RED (below kill — operator should
        consider flipping extras['deactivated']=True)
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
    from eta_engine.obs.weekly_sharpe_gate import run_once

    p = argparse.ArgumentParser(prog="weekly_sharpe_check")
    p.add_argument("--journal", type=Path, default=_DEFAULT_JOURNAL)
    p.add_argument(
        "--last-n",
        type=int,
        default=30,
        help="recent trades per bot (default 30 ~= ~2-4 weeks of fills)",
    )
    p.add_argument(
        "--min-trades",
        type=int,
        default=10,
        help="minimum sample before any non-green verdict (default 10)",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=0.0,
        help="kill threshold; anything below counts as RED (default 0.0)",
    )
    p.add_argument(
        "--review-band",
        type=float,
        default=0.5,
        help="amber band above the kill threshold (default 0.5)",
    )
    p.add_argument("--include-deactivated", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.journal.exists():
        print(
            f"[weekly_sharpe_check] journal not found at {args.journal} -- "
            "skipping (will exit 0; create the journal first)"
        )
        return 0

    journal = DecisionJournal(args.journal)
    out = run_once(
        journal=journal,
        last_n=args.last_n,
        min_trades=args.min_trades,
        threshold=args.threshold,
        review_band=args.review_band,
        write_event=not args.dry_run,
        skip_deactivated=not args.include_deactivated,
    )

    rank = {"green": 0, "amber": 1, "red": 2}
    worst = 0
    print(f"{'bot_id':<22} {'severity':<8} {'n':>4} {'sharpe':>8} {'action':<10} {'reason':<40}")
    print("-" * 100)
    for a in out:
        worst = max(worst, rank[a.severity])
        reason_first = a.reasons[0][:38] if a.reasons else ""
        print(
            f"{a.bot_id:<22} {a.severity.upper():<8} {a.n_trades:>4} "
            f"{a.sharpe:>+8.3f} {a.recommended_action:<10} {reason_first}"
        )
    if args.dry_run:
        print("\n(dry-run: no GRADER events written)")
    return worst


if __name__ == "__main__":
    sys.exit(main())
