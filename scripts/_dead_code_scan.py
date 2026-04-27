"""Find unused public symbols across eta_engine.

Two-pass AST scan over the package(s):

1. **definition pass** -- collect every public class / function / async
   function name with the file it's defined in
2. **reference pass** -- collect every ``Name`` and ``Attribute.attr``
   load referenced anywhere in the package(s) AND in tests/

A symbol is reported as DEAD when:
* it is defined in the package
* it is NOT referenced anywhere in the package OR tests/
* it does not appear in any ``__all__`` literal
* it does not match the operator's known-callable patterns
  (``main``, ``run``, ``execute``, ``handle_*``, ``on_*``)

Exit codes
----------
0 GREEN  -- no dead symbols (or under --threshold)
1 YELLOW -- 1..--threshold-red dead symbols
2 RED    -- > --threshold-red dead symbols
9 setup error

Why this exists
---------------
Modules accumulate cruft: experimental functions that were tested once
and forgotten, helpers that became obsolete after a refactor. Static
detection is conservative -- it only flags symbols that are PROVABLY
unreferenced, so the false-positive rate is low. The operator can
either delete the symbol, add it to ``__all__`` if it's a public API
that's only used externally, or move it under an ``_underscore`` name
if it's truly private.

Caveats
-------
* Reflection (``getattr(mod, "name")``, ``importlib``) defeats this
  scan -- but the operator's codebase is straightforward, no metaclass
  magic.
* Pytest auto-discovery recognizes ``test_*`` functions even when
  they're not directly imported -- handled by the operator-pattern
  filter (``test_*`` is in the known-callable list).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[1]
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
DEFAULT_REFERENCE_DIRS = ["tests", "scripts"]

# Patterns that are commonly callable-via-framework (pytest, CLI, hooks)
KNOWN_CALLABLE_REGEX = re.compile(
    r"^(main|run|execute|handle_.*|on_.*|test_.*|fixture_.*|setup|teardown)$",
)


def _collect_definitions(path: Path) -> list[tuple[str, int, str]]:
    """Return [(name, lineno, kind)] for top-level public defs in this file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return []
    out = []
    for node in tree.body:
        name = getattr(node, "name", None)
        if not name or name.startswith("_"):
            continue
        if isinstance(node, ast.ClassDef):
            out.append((name, node.lineno, "class"))
        elif isinstance(node, ast.AsyncFunctionDef):
            out.append((name, node.lineno, "async-fn"))
        elif isinstance(node, ast.FunctionDef):
            out.append((name, node.lineno, "fn"))
    return out


def _collect_all_literal(path: Path) -> set[str]:
    """Return names found in module-level ``__all__`` list/tuple."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return set()
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            for elt in node.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    names.add(elt.value)
    return names


def _collect_references(path: Path) -> set[str]:
    """Return all Name.id and Attribute.attr accesses (loads)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return set()
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            refs.add(node.id)
        elif isinstance(node, ast.Attribute):
            refs.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                refs.add(alias.name)
                if alias.asname:
                    refs.add(alias.asname)
    return refs


def _walk(packages: list[str], collector: Callable[[Path], Any]) -> dict:
    """Apply collector(path) to every .py in given dirs; aggregate by path."""
    out = {}
    for pkg in packages:
        pkg_dir = ROOT / pkg
        if not pkg_dir.exists():
            continue
        for py in pkg_dir.rglob("*.py"):
            if py.name == "__init__.py":
                continue
            out[py] = collector(py)
    return out


def _walk_refs(dirs: list[str]) -> set[str]:
    """Union of all references across given directories."""
    refs: set[str] = set()
    for d in dirs:
        d_path = ROOT / d
        if not d_path.exists():
            continue
        for py in d_path.rglob("*.py"):
            refs |= _collect_references(py)
    return refs


def _find_dead(
    packages: list[str],
    reference_dirs: list[str],
) -> list[dict]:
    defs_by_file = _walk(packages, _collect_definitions)
    all_literals: set[str] = set()
    for f in defs_by_file:
        all_literals |= _collect_all_literal(f)

    # References from package code (cross-module use)
    pkg_refs = _walk_refs(packages)
    # References from tests + scripts
    ext_refs = _walk_refs(reference_dirs)
    all_refs = pkg_refs | ext_refs | all_literals

    dead: list[dict] = []
    for path, defs in defs_by_file.items():
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        for name, lineno, kind in defs:
            if name in all_refs:
                continue
            if KNOWN_CALLABLE_REGEX.match(name):
                continue
            dead.append(
                {
                    "module": rel,
                    "symbol": name,
                    "lineno": lineno,
                    "kind": kind,
                }
            )
    return dead


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--packages", nargs="+", default=DEFAULT_PACKAGES)
    p.add_argument(
        "--reference-dirs",
        nargs="+",
        default=DEFAULT_REFERENCE_DIRS,
        help="dirs to also scan for references (default: tests scripts)",
    )
    p.add_argument(
        "--threshold-yellow",
        type=int,
        default=1,
        help="dead symbols >= this trigger YELLOW (default 1)",
    )
    p.add_argument(
        "--threshold-red",
        type=int,
        default=20,
        help="dead symbols > this trigger RED (default 20)",
    )
    p.add_argument(
        "--max-show",
        type=int,
        default=30,
        help="cap output rows (default 30)",
    )
    args = p.parse_args(argv)

    dead = _find_dead(args.packages, args.reference_dirs)
    n = len(dead)

    if n == 0:
        print("dead-code-scan: GREEN -- no dead public symbols found")
        return 0
    level = "RED" if n > args.threshold_red else ("YELLOW" if n >= args.threshold_yellow else "GREEN")
    code = {"GREEN": 0, "YELLOW": 1, "RED": 2}[level]
    print(
        f"dead-code-scan: {level} -- {n} dead public symbols across {len({d['module'] for d in dead})} files",
    )
    # Sort by file then lineno
    for d in sorted(dead, key=lambda x: (x["module"], x["lineno"]))[: args.max_show]:
        print(f"  {d['module']}:{d['lineno']:>4}  [{d['kind']:>8}]  {d['symbol']}")
    if n > args.max_show:
        print(f"  ... and {n - args.max_show} more (raise --max-show)")
    return code


if __name__ == "__main__":
    sys.exit(main())
