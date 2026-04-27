"""Rank files by ruff lint debt count. Top N hotspots.

Runs ``ruff check . --statistics`` and ``ruff check . --output-format=json``
and aggregates the violation count per file. Surfaces the worst
offenders so the operator can target cleanup efforts.

Usage
-----
    python scripts/_lint_debt_ranking.py            # top 20 files by debt
    python scripts/_lint_debt_ranking.py --top 50
    python scripts/_lint_debt_ranking.py --by-rule  # rank by rule code instead

Why
---
The Firm has 362 ruff violations across the whole repo. The operator
needs to know "which files do I attack first" -- one big offender is
easier to fix than spreading effort across many.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_ruff_json() -> tuple[int, list[dict]]:
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                ".",
                "--output-format=json",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return (124, [])
    if not proc.stdout:
        return (proc.returncode, [])
    try:
        return (proc.returncode, json.loads(proc.stdout))
    except json.JSONDecodeError:
        return (proc.returncode, [])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--top", type=int, default=20, help="show top N (default 20)")
    p.add_argument("--by-rule", action="store_true", help="rank by rule code instead of by file")
    p.add_argument("--max-yellow", type=int, default=100, help="if total violations > N -> RED (default 100)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    rc, findings = _run_ruff_json()
    if rc != 0 and not findings:
        print("lint-debt-ranking: ERROR -- could not run ruff or parse output")
        return 2
    if not findings:
        print("lint-debt-ranking: GREEN -- 0 violations")
        return 0

    by_file: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    for f in findings:
        try:
            fname = str(Path(f["filename"]).relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            fname = f.get("filename", "?")
        by_file[fname] += 1
        by_rule[f.get("code", "?")] += 1

    if args.json:
        print(
            json.dumps(
                {
                    "total": len(findings),
                    "by_file": dict(by_file.most_common(args.top)),
                    "by_rule": dict(by_rule.most_common(args.top)),
                },
                indent=2,
            )
        )
        return 0

    total = len(findings)
    print(
        f"lint-debt-ranking: {total} total violations across {len(by_file)} file(s), {len(by_rule)} rule(s)",
    )
    print()

    counter = by_rule if args.by_rule else by_file
    label = "rule" if args.by_rule else "file"
    print(f"  Top {args.top} by {label}:")
    print(f"  {'count':>5}  {label}")
    print(f"  {'-' * 5}  {'-' * 50}")
    for name, count in counter.most_common(args.top):
        print(f"  {count:>5}  {name}")

    level = "GREEN" if total == 0 else "RED" if total > args.max_yellow else "YELLOW"
    print(f"\nlint-debt-ranking: {level}")
    return {"GREEN": 0, "YELLOW": 1, "RED": 2}[level]


if __name__ == "__main__":
    sys.exit(main())
