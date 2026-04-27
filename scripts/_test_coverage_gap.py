"""Find source modules that have no matching test file.

Pairs with ``_test_scaffold.py``. Workflow:

    python scripts/_test_coverage_gap.py
    # see list of un-tested modules
    python scripts/_test_scaffold.py <one of them>
    # ... edit the scaffolded test, then commit

Convention
----------
A source file ``<package>/<sub>/<name>.py`` is considered tested when
ANY of these test files exist:

    tests/test_<package>_<sub>_<name>.py
    tests/test_<package>_<name>.py
    tests/<package>/test_<name>.py

(The first form is the ``_test_scaffold.py`` default.) The script
also accepts as evidence ANY test file whose contents import from
``eta_engine.<dotted_path>``.

Skips
-----
* ``__init__.py`` (re-export only)
* Files where the only top-level public symbols are dataclasses
  / NamedTuples / TypedDicts -- pure data with no behavior to test
* Files smaller than ``--min-loc`` non-blank lines (default 20)
* Files starting with ``_`` (private)

Exit codes
----------
0  GREEN  -- no untested modules above min-loc
1  YELLOW -- 1..--max-yellow untested modules
2  RED    -- > --max-yellow untested modules
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
DEFAULT_PACKAGES = [
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


def _candidate_test_paths(src: Path) -> list[Path]:
    """Possible test paths that, if existing, indicate this src is tested."""
    rel = src.relative_to(ROOT)
    parts = list(rel.parts)
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    full_underscore = "_".join(parts)
    candidates = [
        TESTS_DIR / f"test_{full_underscore}.py",
    ]
    if len(parts) >= 2:
        # tests/test_<pkg>_<name>.py
        candidates.append(
            TESTS_DIR / f"test_{parts[0]}_{parts[-1]}.py",
        )
        # tests/<pkg>/test_<name>.py
        candidates.append(
            TESTS_DIR / parts[0] / f"test_{parts[-1]}.py",
        )
    return candidates


def _has_test_via_import(src: Path) -> bool:
    """Return True if ANY tests/*.py imports from eta_engine.<dotted>."""
    rel = src.relative_to(ROOT)
    parts = list(rel.parts)
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    dotted = "eta_engine." + ".".join(parts)
    needle = f"from {dotted}"
    needle_alt = f"import {dotted}"
    if not TESTS_DIR.exists():
        return False
    for tf in TESTS_DIR.rglob("test_*.py"):
        try:
            text = tf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if needle in text or needle_alt in text:
            return True
    return False


def _is_pure_data(src: Path) -> bool:
    """Return True when all top-level symbols are dataclasses/NamedTuples."""
    try:
        tree = ast.parse(src.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return False
    has_class = False
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            has_class = True
            # Look for @dataclass / NamedTuple base / TypedDict base
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
            # Top-level function -> not pure data
            return False
    return has_class


def _loc(src: Path) -> int:
    try:
        return sum(
            1
            for line in src.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    except OSError:
        return 0


def _find_gaps(packages: list[str], min_loc: int) -> list[dict]:
    out = []
    for pkg in packages:
        pkg_dir = ROOT / pkg
        if not pkg_dir.exists():
            continue
        for src in pkg_dir.rglob("*.py"):
            if src.name == "__init__.py":
                continue
            if src.name.startswith("_"):
                continue
            loc = _loc(src)
            if loc < min_loc:
                continue
            if _is_pure_data(src):
                continue
            candidates = _candidate_test_paths(src)
            if any(c.exists() for c in candidates):
                continue
            if _has_test_via_import(src):
                continue
            rel = str(src.relative_to(ROOT)).replace("\\", "/")
            out.append(
                {
                    "module": rel,
                    "loc": loc,
                    "candidates": [str(c.relative_to(ROOT)).replace("\\", "/") for c in candidates],
                }
            )
    out.sort(key=lambda d: -d["loc"])
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--packages", nargs="+", default=DEFAULT_PACKAGES)
    p.add_argument("--min-loc", type=int, default=20)
    p.add_argument(
        "--max-yellow",
        type=int,
        default=10,
        help="more than this many gaps -> RED",
    )
    p.add_argument(
        "--show-candidates",
        action="store_true",
        help="for each gap, print the candidate test paths",
    )
    args = p.parse_args(argv)

    gaps = _find_gaps(args.packages, args.min_loc)
    n = len(gaps)
    if n == 0:
        print(f"test-coverage-gap: GREEN -- no untested modules above {args.min_loc} loc")
        return 0
    level = "RED" if n > args.max_yellow else "YELLOW"
    print(
        f"test-coverage-gap: {level} -- {n} untested modules (min-loc={args.min_loc})",
    )
    for g in gaps:
        print(f"  {g['loc']:>4} loc  {g['module']}")
        if args.show_candidates:
            for c in g["candidates"]:
                print(f"           expected: {c}")
    print(
        "\nNext: python scripts/_test_scaffold.py <module> (scaffolds tests/test_<pkg>_<name>.py)",
    )
    return 1 if level == "YELLOW" else 2


if __name__ == "__main__":
    sys.exit(main())
