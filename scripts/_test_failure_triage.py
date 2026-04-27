"""Run pytest, parse failures, rank by file and error class.

If pytest exits non-zero, this script extracts the FAILED lines and the
short-traceback summaries, then groups them so the operator sees:

    by_file:
        tests/test_foo.py            3 failure(s)
        tests/test_bar.py            1 failure(s)
    by_error:
        AssertionError               2
        AttributeError               1
        ValidationError              1

Exit codes
----------
0  GREEN  -- pytest passed
1  YELLOW -- some failures, all in one file/class (focused)
2  RED    -- failures spread across multiple files (systemic)

Usage
-----
    python scripts/_test_failure_triage.py
    python scripts/_test_failure_triage.py --pattern tests/test_bots*
    python scripts/_test_failure_triage.py --json

Why
---
A 50-line pytest tail tells you SOMETHING failed. This tells you WHAT
is failing in a structured shape so triage takes seconds, not minutes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# pytest short-traceback summary lines look like:
#   FAILED tests/test_foo.py::test_bar - AssertionError: x != y
FAILED_RE = re.compile(
    r"^FAILED\s+(?P<path>[^\s:]+)(?:::[^\s]+)?\s*-\s*(?P<err>[A-Za-z_][\w.]*)",
)
ERROR_RE = re.compile(
    r"^ERROR\s+(?P<path>[^\s:]+)(?:::[^\s]+)?\s*-\s*(?P<err>[A-Za-z_][\w.]*)",
)


def _run_pytest(pattern: str | None) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=line", "--no-header", "--maxfail=200"]
    if pattern:
        cmd.append(pattern)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return (124, "(pytest timed out after 600s)")
    return (proc.returncode, proc.stdout + proc.stderr)


def _parse(output: str) -> tuple[Counter[str], Counter[str], list[str]]:
    by_file: Counter[str] = Counter()
    by_err: Counter[str] = Counter()
    lines: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        m = FAILED_RE.match(line) or ERROR_RE.match(line)
        if not m:
            continue
        path = m.group("path").replace("\\", "/")
        err = m.group("err").rsplit(".", 1)[-1]
        by_file[path] += 1
        by_err[err] += 1
        lines.append(line)
    return by_file, by_err, lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--pattern", help="pytest pattern (default: all)")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    rc, out = _run_pytest(args.pattern)
    by_file, by_err, lines = _parse(out)
    total = sum(by_file.values())

    if args.json:
        print(
            json.dumps(
                {
                    "rc": rc,
                    "total_failures": total,
                    "by_file": dict(by_file.most_common(args.top)),
                    "by_error": dict(by_err.most_common(args.top)),
                    "failures": lines[: args.top * 5],
                },
                indent=2,
            )
        )
        return {0: 0, 1: 1}.get(rc, 2) if rc != 0 else 0

    if rc == 0:
        print("test-failure-triage: GREEN -- pytest passed")
        return 0
    if total == 0:
        print(f"test-failure-triage: YELLOW -- pytest exited {rc} but no FAILED/ERROR lines parsed; tail:")
        print("\n".join(out.splitlines()[-15:]))
        return 1

    n_files = len(by_file)
    level = "RED" if n_files > 1 else "YELLOW"
    print(
        f"test-failure-triage: {level} -- {total} failure(s) across {n_files} file(s), {len(by_err)} error class(es)",
    )
    print()
    print(f"  By file (top {args.top}):")
    for path, c in by_file.most_common(args.top):
        print(f"    {c:>3}  {path}")
    print()
    print(f"  By error (top {args.top}):")
    for err, c in by_err.most_common(args.top):
        print(f"    {c:>3}  {err}")
    if lines:
        print()
        print(f"  First {min(args.top, len(lines))} failure lines:")
        for line in lines[: args.top]:
            print(f"    {line}")
    return 1 if level == "YELLOW" else 2


if __name__ == "__main__":
    sys.exit(main())
