"""Verify every strategy in strategies/ has a corresponding test file.

Sister of _test_coverage_gap.py, but strategy-focused. The Firm's
edge lives in strategies/ -- if a strategy has no test, that's a
priority gap, not just an "untested module".

A strategy ``strategies/<name>.py`` is considered tested if ANY of
these test files exist (matching scripts/_test_scaffold.py conventions):

    tests/test_strategies_<name>.py
    tests/strategies/test_<name>.py

OR if any test file imports ``eta_engine.strategies.<name>``.

Pure-data files (only dataclass/NamedTuple/TypedDict at top level)
and files smaller than --min-loc are skipped, same as test_coverage_gap.

Exit codes
----------
0  GREEN  -- every non-trivial strategy has a test
1  YELLOW -- 1..--max-yellow strategies untested
2  RED    -- > --max-yellow strategies untested

Why
---
A trading firm whose tests don't cover its alpha strategies is
flying blind. The general test_coverage_gap script reports across
all packages; this one zooms in on the part that matters most.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES_DIR = ROOT / "strategies"
TESTS_DIR = ROOT / "tests"


def _candidate_test_paths(strat: Path) -> list[Path]:
    name = strat.stem
    return [
        TESTS_DIR / f"test_strategies_{name}.py",
        TESTS_DIR / "strategies" / f"test_{name}.py",
    ]


def _has_test_via_import(strat: Path) -> bool:
    dotted = f"eta_engine.strategies.{strat.stem}"
    needles = (f"from {dotted}", f"import {dotted}")
    if not TESTS_DIR.exists():
        return False
    return any(
        any(n in tf.read_text(encoding="utf-8", errors="ignore") for n in needles)
        for tf in TESTS_DIR.rglob("test_*.py")
    )


def _is_pure_data(strat: Path) -> bool:
    try:
        tree = ast.parse(strat.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return False
    has_class = False
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            has_class = True
            decorated = any(
                (isinstance(d, ast.Name) and d.id == "dataclass")
                or (isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "dataclass")
                or (isinstance(d, ast.Attribute) and d.attr == "dataclass")
                for d in node.decorator_list
            )
            base_data = any(
                (isinstance(b, ast.Name) and b.id in ("NamedTuple", "TypedDict"))
                or (isinstance(b, ast.Attribute) and b.attr in ("NamedTuple", "TypedDict"))
                for b in node.bases
            )
            if not (decorated or base_data):
                return False
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
    return has_class


def _loc(strat: Path) -> int:
    try:
        return sum(
            1 for ln in strat.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.strip().startswith("#")
        )
    except OSError:
        return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--min-loc", type=int, default=30)
    p.add_argument("--max-yellow", type=int, default=3)
    args = p.parse_args(argv)

    if not STRATEGIES_DIR.exists():
        print(f"strategy-test-pairing: ERROR -- {STRATEGIES_DIR} not found")
        return 2

    untested: list[tuple[str, int]] = []
    counted = 0
    for strat in STRATEGIES_DIR.rglob("*.py"):
        if strat.name == "__init__.py" or strat.name.startswith("_"):
            continue
        loc = _loc(strat)
        if loc < args.min_loc:
            continue
        if _is_pure_data(strat):
            continue
        counted += 1
        if any(c.exists() for c in _candidate_test_paths(strat)):
            continue
        if _has_test_via_import(strat):
            continue
        rel = str(strat.relative_to(ROOT)).replace("\\", "/")
        untested.append((rel, loc))

    untested.sort(key=lambda x: -x[1])
    n = len(untested)
    if n == 0:
        print(
            f"strategy-test-pairing: GREEN -- {counted} strategies, all tested",
        )
        return 0
    level = "RED" if n > args.max_yellow else "YELLOW"
    print(
        f"strategy-test-pairing: {level} -- {n}/{counted} strategies untested",
    )
    for rel, loc in untested:
        print(f"  {loc:>4} loc  {rel}")
    print(
        "\nNext: python scripts/_test_scaffold.py <strategy_path>",
    )
    return 1 if level == "YELLOW" else 2


if __name__ == "__main__":
    sys.exit(main())
