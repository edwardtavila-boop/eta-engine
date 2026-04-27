"""For changed files, list the test files that should run.

Pre-commit ruff catches lint. Pytest sweep catches breakage AFTER you
run it. This script answers: given my staged/unstaged changes, what
TESTS should I be running RIGHT NOW?

Usage
-----
    python scripts/_change_impact.py                    # staged files
    python scripts/_change_impact.py --unstaged         # staged + unstaged
    python scripts/_change_impact.py --since HEAD~3     # since N commits ago
    python scripts/_change_impact.py --run              # actually run pytest on the impacted set
    python scripts/_change_impact.py file1 file2        # explicit file list

Discovery method
----------------
For each changed source file ``<package>/<sub>/<name>.py``:
1. Direct test files matching the convention:
     tests/test_<package>_<sub>_<name>.py
     tests/test_<package>_<name>.py
     tests/<package>/test_<name>.py
2. Tests that import the module:
     `from eta_engine.<package>.<sub>.<name>` or `import eta_engine.<package>.<sub>.<name>`

Test files that change directly are always included.

Output
------
- list of impacted test files (deduped, sorted)
- count summary
- pytest invocation suggestion (or runs it with --run)

Why
---
Operator was running full pytest sweep (~9s for 2255 tests) after
every edit. For tight inner-loop work on one module, the impacted
slice is usually <50 tests and runs in <1s. Speeds up iteration ~10x.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def _staged() -> list[str]:
    out = _run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _unstaged() -> list[str]:
    out = _run(["git", "diff", "--name-only", "--diff-filter=ACMR"])
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _since(ref: str) -> list[str]:
    out = _run(["git", "diff", "--name-only", "--diff-filter=ACMR", f"{ref}..HEAD"])
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _candidate_test_paths(src_rel: str) -> list[Path]:
    parts = src_rel.split("/")
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    full = "_".join(parts)
    out = [TESTS_DIR / f"test_{full}.py"]
    if len(parts) >= 2:
        out.append(TESTS_DIR / f"test_{parts[0]}_{parts[-1]}.py")
        out.append(TESTS_DIR / parts[0] / f"test_{parts[-1]}.py")
    return out


def _dotted(src_rel: str) -> str:
    parts = src_rel.split("/")
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return "eta_engine." + ".".join(parts)


def _tests_importing(dotted: str) -> list[Path]:
    if not TESTS_DIR.exists():
        return []
    needles = (f"from {dotted}", f"import {dotted}")
    out: list[Path] = []
    for tf in TESTS_DIR.rglob("test_*.py"):
        try:
            text = tf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(n in text for n in needles):
            out.append(tf)
    return out


def _impact(changed: list[str]) -> set[Path]:
    impacted: set[Path] = set()
    for f in changed:
        if not f.endswith(".py"):
            continue
        # Test files that change are always included
        if f.startswith("tests/"):
            p = ROOT / f
            if p.exists():
                impacted.add(p)
            continue
        # Source file -> find tests
        for cand in _candidate_test_paths(f):
            if cand.exists():
                impacted.add(cand)
        for tf in _tests_importing(_dotted(f)):
            impacted.add(tf)
    return impacted


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT)).replace("\\", "/")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("paths", nargs="*", help="explicit file list (overrides git modes)")
    p.add_argument("--unstaged", action="store_true", help="include unstaged changes")
    p.add_argument("--since", default=None, help="git ref: changes since this ref")
    p.add_argument("--run", action="store_true", help="invoke pytest on the impacted set")
    p.add_argument("--quiet", "-q", action="store_true", help="only print pytest invocation")
    args = p.parse_args(argv)

    if args.paths:
        changed = list(args.paths)
    elif args.since:
        changed = _since(args.since)
    elif args.unstaged:
        changed = list({*_staged(), *_unstaged()})
    else:
        changed = _staged()

    if not changed:
        print("change-impact: nothing changed (try --unstaged or --since HEAD~N)")
        return 0

    if not args.quiet:
        print(f"change-impact: {len(changed)} changed file(s)")
        for f in sorted(changed):
            print(f"  ~ {f}")

    impacted = sorted(_impact(changed), key=_rel)
    if not impacted:
        print("change-impact: no test files impacted (sources may be untested)")
        return 0

    if not args.quiet:
        print(f"\nchange-impact: {len(impacted)} impacted test file(s)")
        for tf in impacted:
            print(f"  > {_rel(tf)}")

    cmd = [sys.executable, "-m", "pytest", "-x", "-q", *(_rel(tf) for tf in impacted)]
    print()
    print("  Suggested:  " + " ".join(cmd[1:]))

    if args.run:
        print("\nchange-impact: running pytest on impacted set ...")
        result = subprocess.run(cmd, cwd=str(ROOT), check=False)
        return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
