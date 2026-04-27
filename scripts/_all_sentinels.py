"""Master runner: every sentinel, one report.

Single entry point that fans out to every drift-detector script, captures
exit codes + last-line summaries, and renders a one-screen status table.

Sentinels run (in order)
------------------------
1. coverage-drift       -- per-module pytest coverage ratchet
2. docstring-audit      -- public-symbol docstring ratchet
3. dead-code            -- two-pass AST orphan detector
4. test-gap             -- modules with no test file
5. fleet-invariants     -- cross-bot AST drift checker
6. roadmap-drift        -- roadmap_state.json claim verifier
7. bot-health-L0        -- bot module imports cleanly
8. bot-health-L1        -- bot class instantiates

Output
------
A status table with color-coded labels::

    coverage-drift     GREEN   no regression
    fleet-invariants   YELLOW  1 violation (CryptoSeedBot retrospective)
    roadmap-drift      YELLOW  1 drift (tests_passing stale by +64)
    bot-health-L0      GREEN   6 bots pass

    Overall: YELLOW  (sentinels: 6 GREEN, 2 YELLOW, 0 RED)

Exit codes
----------
0  GREEN  -- every sentinel returned 0
1  YELLOW -- at least one returned 1, none returned 2
2  RED    -- at least one returned 2

Usage
-----
    python scripts/_all_sentinels.py            # full sweep
    python scripts/_all_sentinels.py --fast     # skip pytest-collecting sentinels
    python scripts/_all_sentinels.py --json     # emit a JSON report

Why
---
Single command for "is anything drifting RIGHT NOW" — useful as a
pre-commit gate, end-of-day check, or as the body of a cloud cron.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


@dataclass
class SentinelResult:
    name: str
    cmd: list[str]
    exit_code: int
    duration_s: float
    summary_line: str
    fast: bool = True
    skipped: bool = False
    full_output: str = field(default="", repr=False)


# (label, [argv after `python`], fast?)
# fast=True  -> always runs
# fast=False -> only runs without --fast (avoids long pytest collects)
SENTINELS: list[tuple[str, list[str], bool]] = [
    ("coverage-drift", ["scripts/_coverage_drift.py", "--no-run"], True),
    ("docstring-audit", ["scripts/_docstring_audit.py"], True),
    ("dead-code", ["scripts/_dead_code_scan.py"], True),
    ("test-gap", ["scripts/_test_coverage_gap.py"], True),
    ("fleet-invariants", ["scripts/_fleet_invariants.py"], True),
    ("roadmap-drift", ["scripts/_roadmap_drift.py", "--no-pytest"], True),
    ("roadmap-drift+pytest", ["scripts/_roadmap_drift.py"], False),
    ("bot-health-L0", ["scripts/_bot_health_probe.py", "--level", "L0"], True),
    ("bot-health-L1", ["scripts/_bot_health_probe.py", "--level", "L1"], True),
    ("secret-audit", ["scripts/_secret_audit.py"], True),
    ("dependency-drift", ["scripts/_dependency_drift.py"], True),
    ("complexity", ["scripts/_complexity_hotspots.py", "--threshold", "20", "--top", "10"], True),
    ("strategy-pairing", ["scripts/_strategy_test_pairing.py"], True),
    ("orphan-files", ["scripts/_orphan_files.py"], True),
    ("import-graph", ["scripts/_import_graph.py"], True),
    ("long-tests", ["scripts/_long_test_finder.py", "--top", "10", "--max-yellow-ms", "500"], False),
]


VERDICT_KEYWORDS = ("GREEN", "YELLOW", "RED", "SKIP", "data-missing")
HINT_PREFIXES = ("- ", "* ", "Next:", "Fix:", "Suggested:", "Tip:", "  -", "  *")


def _last_summary_line(output: str) -> str:
    """Find the final summary line.

    Preference order:
      1. last line containing a verdict keyword (GREEN/YELLOW/RED/SKIP)
      2. last non-empty non-bullet, non-hint line
      3. last non-empty line
    """
    lines = [ln.rstrip() for ln in output.splitlines() if ln.strip()]
    if not lines:
        return "(no output)"
    for line in reversed(lines):
        if any(k in line for k in VERDICT_KEYWORDS):
            return line.strip()
    for line in reversed(lines):
        s = line.strip()
        if not s.startswith(HINT_PREFIXES):
            return s
    return lines[-1]


def _run_one(name: str, argv: list[str]) -> SentinelResult:
    cmd = [sys.executable, *argv]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        out = proc.stdout + proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        out = "(timed out after 180s)"
        exit_code = 2
    duration = time.monotonic() - t0
    return SentinelResult(
        name=name,
        cmd=cmd,
        exit_code=exit_code,
        duration_s=duration,
        summary_line=_last_summary_line(out),
        full_output=out,
    )


def _label_for(exit_code: int) -> str:
    # Convention: 0 GREEN, 1 YELLOW, 2 RED, 9 data-missing (treated as SKIP).
    if exit_code == 0:
        return "GREEN"
    if exit_code == 1:
        return "YELLOW"
    if exit_code == 2:
        return "RED"
    if exit_code == 9:
        return "SKIP "  # data-missing -> not a real failure
    return f"EXIT={exit_code}"


def _verdict(results: list[SentinelResult]) -> tuple[str, dict[str, int]]:
    counts = {"GREEN": 0, "YELLOW": 0, "RED": 0, "SKIP": 0}
    for r in results:
        if r.skipped:
            counts["SKIP"] += 1
            continue
        lbl = _label_for(r.exit_code).strip()
        counts[lbl] = counts.get(lbl, 0) + 1
    if counts.get("RED", 0):
        return ("RED", counts)
    if counts.get("YELLOW", 0):
        return ("YELLOW", counts)
    return ("GREEN", counts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--fast", action="store_true", help="skip slow sentinels (pytest collects)")
    p.add_argument("--json", action="store_true", help="emit JSON report instead of table")
    p.add_argument("--verbose", "-v", action="store_true", help="dump full output of each sentinel")
    args = p.parse_args(argv)

    results: list[SentinelResult] = []
    for name, sub_argv, is_fast in SENTINELS:
        if args.fast and not is_fast:
            results.append(
                SentinelResult(
                    name=name,
                    cmd=[sys.executable, *sub_argv],
                    exit_code=0,
                    duration_s=0.0,
                    summary_line="(skipped --fast)",
                    fast=is_fast,
                    skipped=True,
                )
            )
            continue
        results.append(_run_one(name, sub_argv))

    verdict, counts = _verdict(results)

    if args.json:
        payload = {
            "verdict": verdict,
            "counts": counts,
            "results": [asdict(r) for r in results],
        }
        print(json.dumps(payload, indent=2))
        return {"GREEN": 0, "YELLOW": 1, "RED": 2}[verdict]

    # Table render
    print(f"=== EVOLUTIONARY TRADING ALGO Sentinel Sweep ===  ({len(results)} sentinels)\n")
    name_w = max(len(r.name) for r in results)
    for r in results:
        label = "SKIP " if r.skipped else _label_for(r.exit_code).ljust(6)
        print(f"  {r.name.ljust(name_w)}  {label}  {r.summary_line}")

    print()
    print(
        f"  Overall: {verdict}  "
        f"(sentinels: {counts.get('GREEN', 0)} GREEN, "
        f"{counts.get('YELLOW', 0)} YELLOW, {counts.get('RED', 0)} RED)",
    )
    elapsed = sum(r.duration_s for r in results)
    print(f"  Elapsed: {elapsed:.1f}s")

    if args.verbose:
        print("\n--- Full output ---")
        for r in results:
            if r.skipped:
                continue
            print(f"\n===== {r.name} (exit={r.exit_code}, {r.duration_s:.1f}s) =====")
            print(r.full_output)

    return {"GREEN": 0, "YELLOW": 1, "RED": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
