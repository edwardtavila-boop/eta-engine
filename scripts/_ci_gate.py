"""End-to-end gate: ruff + pytest + sentinels. Exits non-zero on any failure.

Wraps the full pre-merge / pre-push check sequence into one command.
Suitable for use as the body of a CI workflow, a pre-push git hook,
or as the operator's "is this commit safe to push?" verifier.

Stages
------
1. ruff-check    : `python -m ruff check` on STAGED .py files (matches
                   pre-commit). Use --strict-ruff to lint the whole repo.
2. pytest-fast   : `python -m pytest -x -q`
3. sentinels     : `python scripts/_all_sentinels.py --fast`

Failure of any stage halts and reports the failing stage. Success
prints a one-line green verdict.

Exit codes
----------
0  ALL GREEN -- safe to commit/push
1  ruff failed
2  pytest failed
3  sentinels returned RED (or YELLOW with --strict)

Usage
-----
    python scripts/_ci_gate.py             # default: green or red verdict
    python scripts/_ci_gate.py --strict    # yellow sentinels also fail
    python scripts/_ci_gate.py --no-pytest # skip pytest stage
    python scripts/_ci_gate.py --no-sent   # skip sentinel stage

Why
---
One key for the operator's pre-push muscle memory. Replaces:
    python -m ruff check . && python -m pytest -x -q && python scripts/_all_sentinels.py --fast
with:
    python scripts/_ci_gate.py
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str]) -> tuple[int, str, float]:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        out = proc.stdout + proc.stderr
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        out = "(timed out after 300s)"
        rc = 124
    return (rc, out, time.monotonic() - t0)


def _last_lines(s: str, n: int = 8) -> str:
    return "\n".join(("    " + ln) for ln in s.splitlines()[-n:])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--strict", action="store_true", help="yellow sentinels also fail")
    p.add_argument("--strict-ruff", action="store_true", help="lint the entire repo (default: staged files only)")
    p.add_argument("--no-pytest", action="store_true", help="skip pytest stage")
    p.add_argument("--no-sent", action="store_true", help="skip sentinel stage")
    p.add_argument("--no-ruff", action="store_true", help="skip ruff stage")
    args = p.parse_args(argv)

    print("=== CI Gate ===")

    if not args.no_ruff:
        if args.strict_ruff:
            ruff_targets = ["."]
            mode_label = "whole repo"
        else:
            rc, files, _ = _run(
                [
                    "git",
                    "diff",
                    "--cached",
                    "--name-only",
                    "--diff-filter=ACMR",
                ]
            )
            ruff_targets = [ln.strip() for ln in files.splitlines() if ln.strip().endswith(".py")]
            mode_label = f"{len(ruff_targets)} staged file(s)"
        if ruff_targets:
            print(f"\n[1/3] ruff check ({mode_label})...")
            rc, out, dt = _run([sys.executable, "-m", "ruff", "check", *ruff_targets])
            if rc != 0:
                print(f"  FAIL ({dt:.1f}s)")
                print(_last_lines(out))
                return 1
            print(f"  OK ({dt:.1f}s)")
        else:
            print("\n[1/3] ruff check (staged files): SKIP -- nothing staged")

    if not args.no_pytest:
        print("\n[2/3] pytest -x -q...")
        rc, out, dt = _run([sys.executable, "-m", "pytest", "-x", "-q", "--no-header"])
        if rc != 0:
            print(f"  FAIL ({dt:.1f}s)")
            print(_last_lines(out, 12))
            return 2
        # Pull the summary line
        summary = next(
            (ln for ln in reversed(out.splitlines()) if " passed" in ln or " failed" in ln),
            "",
        )
        print(f"  OK ({dt:.1f}s)  {summary}")

    if not args.no_sent:
        print("\n[3/3] sentinels (fast)...")
        rc, out, dt = _run([sys.executable, "scripts/_all_sentinels.py", "--fast"])
        # 0 GREEN, 1 YELLOW, 2 RED
        if rc == 2:
            print(f"  RED ({dt:.1f}s)")
            print(_last_lines(out, 14))
            return 3
        if rc == 1 and args.strict:
            print(f"  YELLOW ({dt:.1f}s) -- failing under --strict")
            print(_last_lines(out, 14))
            return 3
        verdict = "GREEN" if rc == 0 else "YELLOW"
        # Get the overall line
        verdict_line = next(
            (ln for ln in reversed(out.splitlines()) if "Overall:" in ln),
            f"verdict={verdict}",
        )
        print(f"  {verdict} ({dt:.1f}s)  {verdict_line.strip()}")

    print("\nCI Gate: ALL GREEN -- safe to commit/push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
