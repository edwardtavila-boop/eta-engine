"""
EVOLUTIONARY TRADING ALGO  //  scripts.fleet_corr_check
=======================================================
Fleet-correlation watchdog runner. Walks every
``extras["fleet_corr_partner"]`` pair declared in the per-bot
strategy registry, loads recent trades from the decision journal,
runs ``assess_fleet_correlation``, and writes one ``GRADER`` event
per pair.

Cron-friendly. Recommend a 30-minute scheduled task — matches the
drift-monitor cadence::

    schtasks /Create /SC MINUTE /MO 30 /TN ETA-FleetCorrCheck /TR \\
        "python -m eta_engine.scripts.fleet_corr_check"

Exit code:
  * 0 = every pair is GREEN
  * 1 = at least one pair is AMBER
  * 2 = at least one pair is RED

When no partner pairs are declared in the registry, the script
no-ops with a clear message and exits 0 — that's the right
behaviour before any bot pair has been tagged.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


_DEFAULT_JOURNAL = ROOT / "docs" / "decision_journal.jsonl"


def main() -> int:
    from eta_engine.obs.decision_journal import DecisionJournal
    from eta_engine.obs.fleet_correlation_watchdog import (
        _partner_pairs,
        run_once,
    )

    p = argparse.ArgumentParser(prog="fleet_corr_check")
    p.add_argument("--journal", type=Path, default=_DEFAULT_JOURNAL)
    p.add_argument(
        "--last-n", type=int, default=50,
        help="recent trades per bot to consider (default 50)",
    )
    p.add_argument(
        "--min-paired", type=int, default=10,
        help="minimum paired trades before a non-green verdict (default 10)",
    )
    p.add_argument(
        "--amber-rho", type=float, default=0.5,
        help="amber threshold (default 0.5)",
    )
    p.add_argument(
        "--red-rho", type=float, default=0.7,
        help="red threshold (default 0.7)",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    pairs = _partner_pairs()
    if not pairs:
        print(
            "[fleet_corr_check] no fleet_corr_partner pairs declared in "
            "the per-bot registry -- skipping"
        )
        return 0

    if not args.journal.exists():
        print(
            f"[fleet_corr_check] journal not found at {args.journal} -- "
            "skipping (will exit 0; create the journal first)"
        )
        return 0

    journal = DecisionJournal(args.journal)
    assessments = run_once(
        journal=journal,
        last_n=args.last_n,
        min_paired=args.min_paired,
        amber_rho=args.amber_rho,
        red_rho=args.red_rho,
        write_event=not args.dry_run,
    )

    rank = {"green": 0, "amber": 1, "red": 2}
    worst = 0
    print(
        f"{'pair':<32} {'severity':<8} {'n':>4} {'rho':>7} "
        f"{'action':<18} {'reason':<40}"
    )
    print("-" * 110)
    for a in assessments:
        worst = max(worst, rank[a.severity])
        pair_str = f"{a.bot_a}+{a.bot_b}"[:30]
        reason_first = a.reasons[0][:38] if a.reasons else ""
        print(
            f"{pair_str:<32} {a.severity.upper():<8} {a.n_paired:>4} "
            f"{a.rho:>+7.3f} {a.recommended_action:<18} {reason_first}"
        )
    if args.dry_run:
        print("\n(dry-run: no GRADER events written)")
    return worst


if __name__ == "__main__":
    sys.exit(main())
