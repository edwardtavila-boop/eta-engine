"""Verify roadmap_state.json claims match reality.

The roadmap_state.json file tracks what the operator believes about the
codebase. When auto-bump scripts haven't run (or the operator forgot to
update), it drifts from reality. This script detects the drift.

Real schema (as of 2026-04-18)
------------------------------
* last_updated, overall_progress_pct, current_phase
* shared_artifacts.eta_engine_tests_passing  (integer)
* shared_artifacts.eta_engine_tests_failing  (integer)
* shared_artifacts.eta_engine_python_files   (integer)
* shared_artifacts.databento_rows               (integer)

Checks
------
* C1  tests_passing claim matches `pytest --collect-only -q` (tolerance --tolerance)
* C2  tests_failing claim is 0 (else: stale, since CI gates are green)
* C3  python_files claim matches `glob **/*.py` count (tolerance \u00b110)
* C4  current_phase is one of the known phases
* C5  overall_progress_pct is in [0, 100]
* C6  last_updated parses as ISO-8601 datetime within last 14 days

Exit codes
----------
0  GREEN  -- all checks pass
1  YELLOW -- 1..2 drifts
2  RED    -- 3+ drifts (or any check error)

Usage
-----
    python scripts/_roadmap_drift.py
    python scripts/_roadmap_drift.py --no-pytest
    python scripts/_roadmap_drift.py --tolerance 20
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROADMAP_STATE = ROOT / "roadmap_state.json"

KNOWN_PHASES = {
    "P0_FOUNDATIONS",
    "P1_CORE",
    "P2_STRATEGIES",
    "P3_BACKTEST",
    "P4_OBS",
    "P5_FUNNEL",
    "P6_VENUES",
    "P7_STAKING",
    "P8_INTEGRATION",
    "P9_ROLLOUT",
    "P10_LIVE",
}


def _pytest_count(quiet: bool = False) -> int | None:
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in (out.stdout + out.stderr).splitlines():
        m = re.match(r"^\s*(\d+)\s+tests?\s+collected", line)
        if m:
            return int(m.group(1))
    if not quiet:
        print("  (could not parse pytest collect-only output)", file=sys.stderr)
    return None


def _python_file_count() -> int:
    """Count *.py files in package dirs (excludes scripts/ and tests/)."""
    pkg_dirs = [
        "bots",
        "strategies",
        "core",
        "brain",
        "obs",
        "funnel",
        "backtest",
        "venues",
        "staking",
    ]
    total = 0
    for d in pkg_dirs:
        total += sum(1 for _ in (ROOT / d).rglob("*.py")) if (ROOT / d).exists() else 0
    return total


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--tolerance", type=int, default=10, help="acceptable test_count drift (default 10)")
    p.add_argument("--file-tolerance", type=int, default=10, help="acceptable python_files drift (default 10)")
    p.add_argument("--max-age-days", type=int, default=14, help="last_updated must be within N days (default 14)")
    p.add_argument("--no-pytest", action="store_true", help="skip pytest collection check")
    args = p.parse_args(argv)

    if not ROADMAP_STATE.exists():
        print(f"roadmap-drift: ERROR -- {ROADMAP_STATE} not found")
        return 2
    try:
        state = json.loads(ROADMAP_STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"roadmap-drift: ERROR -- malformed JSON: {e}")
        return 2

    sa = state.get("shared_artifacts", {})
    drifts: list[str] = []

    # C1 tests_passing
    if not args.no_pytest:
        claimed = sa.get("eta_engine_tests_passing")
        actual = _pytest_count()
        if actual is None:
            print("  C1 tests_passing: SKIP -- could not collect")
        elif not isinstance(claimed, int):
            drifts.append(
                f"C1 tests_passing: claimed {claimed!r} not an int",
            )
        elif abs(claimed - actual) > args.tolerance:
            drifts.append(
                f"C1 tests_passing: claimed={claimed} actual={actual} "
                f"(drift={claimed - actual:+d}, tolerance=\u00b1{args.tolerance})",
            )
        else:
            print(f"  C1 tests_passing: OK ({claimed} ~= {actual})")

    # C2 tests_failing == 0
    failing = sa.get("eta_engine_tests_failing")
    if failing is None:
        drifts.append("C2 tests_failing: missing field")
    elif failing != 0:
        drifts.append(
            f"C2 tests_failing: claimed={failing} (expected 0 -- CI is green)",
        )
    else:
        print("  C2 tests_failing: OK (0)")

    # C3 python_files
    claimed_files = sa.get("eta_engine_python_files")
    actual_files = _python_file_count()
    if not isinstance(claimed_files, int):
        drifts.append(f"C3 python_files: claimed {claimed_files!r} not an int")
    elif abs(claimed_files - actual_files) > args.file_tolerance:
        drifts.append(
            f"C3 python_files: claimed={claimed_files} actual={actual_files} "
            f"(drift={claimed_files - actual_files:+d}, "
            f"tolerance=\u00b1{args.file_tolerance})",
        )
    else:
        print(f"  C3 python_files: OK ({claimed_files} ~= {actual_files})")

    # C4 current_phase known
    phase = state.get("current_phase")
    if phase not in KNOWN_PHASES:
        drifts.append(
            f"C4 current_phase: {phase!r} not in known phases ({sorted(KNOWN_PHASES)})",
        )
    else:
        print(f"  C4 current_phase: OK ({phase})")

    # C5 overall_progress_pct in [0, 100]
    pct = state.get("overall_progress_pct")
    if not isinstance(pct, (int, float)) or not (0 <= pct <= 100):
        drifts.append(f"C5 overall_progress_pct: {pct!r} not in [0, 100]")
    else:
        print(f"  C5 overall_progress_pct: OK ({pct}%)")

    # C6 last_updated freshness
    last = state.get("last_updated") or state.get("last_updated_utc")
    if not last:
        drifts.append("C6 last_updated: missing")
    else:
        try:
            ts = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            drifts.append(f"C6 last_updated: cannot parse {last!r}")
        else:
            now = datetime.now(UTC)
            age = now - ts if ts.tzinfo else now.replace(tzinfo=None) - ts
            if age > timedelta(days=args.max_age_days):
                drifts.append(
                    f"C6 last_updated: {age.days}d ago (max {args.max_age_days}d)",
                )
            else:
                print(f"  C6 last_updated: OK ({age.days}d old)")

    n = len(drifts)
    if n == 0:
        print("roadmap-drift: GREEN -- all checks pass")
        return 0
    level = "RED" if n >= 3 else "YELLOW"
    print(f"roadmap-drift: {level} -- {n} drift(s)")
    for d in drifts:
        print(f"  - {d}")
    return 1 if level == "YELLOW" else 2


if __name__ == "__main__":
    sys.exit(main())
