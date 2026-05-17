"""
EVOLUTIONARY TRADING ALGO  //  scripts.schedule_weekly_review
=================================================
Cadence wrapper for scripts.weekly_review.

Decision #14: Sunday 20:00 ET weekly review.
Decision #15: firm board re-engages weekly + on any RED gate.

This script:
  1. Verifies schedule fires at the right time (guard mode).
  2. Runs the weekly_review for BOTH tier A and tier B.
  3. On any RED preflight gate or new kill-log entry, forces firm re-engage.
  4. Emits the cron/Task Scheduler commands to install it.

Usage
-----
  python -m eta_engine.scripts.schedule_weekly_review           # run now
  python -m eta_engine.scripts.schedule_weekly_review --emit-cron
  python -m eta_engine.scripts.schedule_weekly_review --emit-task-scheduler

Cron schedule (decision #14)
  0 20 * * 0  America/New_York  apex weekly review

Windows Task Scheduler equivalent
  Weekly · Sunday · 20:00 ET

Exit codes:
  0 — review completed (GO or MODIFY)
  2 — review completed with KILL verdict
  3 — scheduler guard fired too early / too late (won't run)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from eta_engine.scripts import workspace_roots

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent

TZ_ET = ZoneInfo("America/New_York")
CRON_LINE = "0 20 * * 0"  # Sunday 20:00 ET
TASK_NAME = "EtaEngineWeeklyReview"


def _within_window(now_et: datetime, tolerance_minutes: int = 30) -> bool:
    """Return True if now is within ± tolerance of Sunday 20:00 ET."""
    # Sunday == weekday 6
    if now_et.weekday() != 6:
        return False
    target_minutes = 20 * 60  # 20:00 in minutes-of-day
    now_minutes = now_et.hour * 60 + now_et.minute
    return abs(now_minutes - target_minutes) <= tolerance_minutes


def _should_force_firm_reengage() -> tuple[bool, str]:
    """Decision #15: re-engage on RED preflight gate or new kill-log entry."""
    pf = workspace_roots.default_preflight_dryrun_report_path()
    if pf.exists():
        try:
            raw = json.loads(pf.read_text())
            if str(raw.get("overall", "")).upper() == "ABORT":
                return True, "preflight RED"
        except Exception:
            pass
    kl = workspace_roots.default_kill_log_path()
    if kl.exists():
        try:
            raw = json.loads(kl.read_text())
            entries = raw.get("entries") if isinstance(raw, dict) else raw
            n = len(entries) if isinstance(entries, list) else 0
            # Compare vs last review's kill count, if present
            wr = _weekly_review_latest_path()
            if wr.exists():
                last = json.loads(wr.read_text())
                last_n = int(last.get("kill_log_entries_at_time", 0))
                if n > last_n:
                    return True, f"new kill-log entries: {last_n} -> {n}"
        except Exception:
            pass
    return False, ""


def _weekly_review_latest_path() -> Path:
    """Prefer canonical weekly review latest, with docs fallback."""
    canonical = ROOT.parent / "var" / "eta_engine" / "state" / "weekly_review" / "weekly_review_latest.json"
    legacy = ROOT / "docs" / "weekly_review_latest.json"
    if canonical.exists() or not legacy.exists():
        return canonical
    return legacy


def _run_weekly_review(tier: str, force_engage: bool) -> int:
    cmd = [
        sys.executable,
        "-m",
        "eta_engine.scripts.weekly_review",
        "--tier",
        tier,
        "--auto",
    ]
    if not force_engage:
        # Still engages — weekly_review always runs the board.
        pass
    print(f"    $ {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
    return r.returncode


def emit_cron() -> str:
    return (
        f"# EVOLUTIONARY TRADING ALGO weekly review — decision #14\n"
        f"# Sunday 20:00 America/New_York\n"
        f"{CRON_LINE} cd {REPO_ROOT} && "
        f"/usr/bin/env python -m eta_engine.scripts.schedule_weekly_review "
        f">> logs/eta_engine/weekly_review_cron.log 2>&1\n"
    )


def emit_task_scheduler() -> str:
    exe = sys.executable
    return (
        f"# EVOLUTIONARY TRADING ALGO weekly review — Windows Task Scheduler\n"
        f"schtasks /Create /SC WEEKLY /D SUN /ST 20:00 /TN {TASK_NAME} "
        f'/TR "\\"{exe}\\" -m eta_engine.scripts.schedule_weekly_review" '
        f"/RL HIGHEST /F\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="APEX weekly review schedule guard")
    ap.add_argument("--tier", default="BOTH", choices=["A", "B", "BOTH"])
    ap.add_argument(
        "--no-window-guard", action="store_true", help="Bypass the Sunday 20:00 ET window check (manual run)"
    )
    ap.add_argument("--emit-cron", action="store_true")
    ap.add_argument("--emit-task-scheduler", action="store_true")
    args = ap.parse_args()

    if args.emit_cron:
        print(emit_cron())
        return 0
    if args.emit_task_scheduler:
        print(emit_task_scheduler())
        return 0

    now_utc = datetime.now(UTC)
    now_et = now_utc.astimezone(TZ_ET)
    in_window = _within_window(now_et)

    print("EVOLUTIONARY TRADING ALGO -- schedule_weekly_review")
    print("=" * 64)
    print(f"now_utc      : {now_utc.isoformat()}")
    print(f"now_et       : {now_et.isoformat()}")
    print(f"sun_20:00_et : within_window={in_window}")
    print(f"tier         : {args.tier}")

    if not (in_window or args.no_window_guard):
        print("not within Sunday 20:00 ET window — skipping. (--no-window-guard to override)")
        return 3

    force, reason = _should_force_firm_reengage()
    print(f"force_reengage: {force}  ({reason or 'no extra trigger'})")
    print("-" * 64)

    rcs: list[int] = []
    for tier in ["A", "B"] if args.tier == "BOTH" else [args.tier]:
        print(f"-> running weekly_review --tier {tier}")
        rcs.append(_run_weekly_review(tier, force_engage=force))

    print("=" * 64)
    print(f"tier_returncodes: {rcs}")
    # Return the worst return code (2 > 0)
    return max(rcs) if rcs else 0


if __name__ == "__main__":
    sys.exit(main())
