"""Audit public symbols for missing docstrings.

Walks the eta_engine package(s), parses each .py file with ``ast``,
and lists public classes / functions / async functions that have no
docstring. Skips:

* Private symbols (name starts with ``_``)
* ``__init__.py`` files (usually pure re-exports)
* Files under ``tests/``
* Symbols decorated with ``@pytest.fixture`` or ``@property``
* Trivial one-liner functions (single statement that's a return/pass)

Optional ``--ratchet`` mode mirrors the coverage drift pattern:
persists the per-module count of un-documented public symbols at
``docs/docstring_baseline.json`` and alerts when a module gets WORSE
(more missing docstrings than its baseline). Otherwise the script
just prints the current list.

Why this exists
---------------
Operator's strategy modules are extremely well-documented; older
utility / brain modules are not. A weekly audit nudges the operator
toward documenting public surfaces as they touch them, without
demanding an all-at-once cleanup.

Exit codes
----------
0  GREEN  -- no regressions (or no ratchet mode)
1  YELLOW -- new undocumented symbols found vs baseline
2  RED    -- baseline doubled or more (suggests bulk untested merge)
9  setup error
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "docs" / "docstring_baseline.json"
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


def _is_skippable_decorator(dec: ast.expr) -> bool:
    """Skip @property, @pytest.fixture, @staticmethod, @classmethod."""
    if isinstance(dec, ast.Name):
        return dec.id in ("property", "staticmethod", "classmethod")
    if isinstance(dec, ast.Attribute):
        # @pytest.fixture / @something.fixture
        return dec.attr in ("fixture", "property")
    if isinstance(dec, ast.Call):
        return _is_skippable_decorator(dec.func)
    return False


def _is_trivial(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """One-statement return/pass/ellipsis -- doesn't need a docstring."""
    if len(node.body) != 1:
        return False
    stmt = node.body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Return) and stmt.value is None:
        return True
    # docstring-only or `...` body
    return bool(isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))


def _audit_file(path: Path) -> list[dict]:
    """Return list of {symbol, kind, lineno} for public missing-docstring symbols."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return []
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            kind = "class"
        elif isinstance(node, ast.AsyncFunctionDef):
            kind = "async-fn"
        elif isinstance(node, ast.FunctionDef):
            kind = "fn"
        else:
            continue
        name = node.name
        if name.startswith("_"):
            continue
        # Skip dunders (already implicitly documented by their contract)
        if name.startswith("__") and name.endswith("__"):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_is_skippable_decorator(d) for d in node.decorator_list):
                continue
            if _is_trivial(node):
                continue
        if ast.get_docstring(node):
            continue
        missing.append({"symbol": name, "kind": kind, "lineno": node.lineno})
    return missing


def _walk_packages(packages: list[str]) -> dict[str, list[dict]]:
    """Return {relative_path: [missing dicts]} for each .py file in given packages."""
    out: dict[str, list[dict]] = {}
    for pkg in packages:
        pkg_dir = ROOT / pkg
        if not pkg_dir.exists():
            continue
        for py in pkg_dir.rglob("*.py"):
            if py.name == "__init__.py":
                continue
            if "tests" in py.parts:
                continue
            missing = _audit_file(py)
            if missing:
                rel = str(py.relative_to(ROOT)).replace("\\", "/")
                out[rel] = missing
    return out


def _classify(prev_count: int | None, cur_count: int) -> tuple[str, int]:
    """Compare to baseline. Returns (level, delta)."""
    if prev_count is None:
        return ("SEED", cur_count)
    delta = cur_count - prev_count
    if cur_count >= max(prev_count * 2, prev_count + 10):
        return ("RED", delta)
    if delta > 0:
        return ("YELLOW", delta)
    return ("GREEN", delta)


def _severity(level: str) -> int:
    return {"SEED": 0, "GREEN": 0, "YELLOW": 1, "RED": 2}.get(level, 0)


def _evaluate(
    current: dict[str, list[dict]],
    baseline: dict,
) -> tuple[list[dict], dict]:
    new_baseline = {
        "per_module": dict(baseline.get("per_module", {})),
        "samples": int(baseline.get("samples", 0)) + 1,
        "last_updated": datetime.now(UTC).isoformat(),
    }
    diagnostics = []
    for module, missing_list in current.items():
        cur_count = len(missing_list)
        prev = baseline.get("per_module", {}).get(module)
        level, delta = _classify(prev, cur_count)
        diagnostics.append(
            {
                "module": module,
                "level": level,
                "delta": delta,
                "current": cur_count,
                "baseline": prev,
                "missing": missing_list,
            }
        )
        # Ratchet DOWNWARDS only -- once we improve, hold the line
        if prev is None:
            new_baseline["per_module"][module] = cur_count
        else:
            new_baseline["per_module"][module] = min(cur_count, int(prev))
    # Modules that were in baseline but disappeared (deleted) -- drop them
    return diagnostics, new_baseline


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    p.add_argument(
        "--packages",
        nargs="+",
        default=DEFAULT_PACKAGES,
        help="package directories to walk (default: all top-level eta_engine pkgs)",
    )
    p.add_argument(
        "--list-only",
        action="store_true",
        help="just print all missing docstrings, no ratchet comparison",
    )
    p.add_argument("--no-update", action="store_true")
    p.add_argument(
        "--max-show",
        type=int,
        default=10,
        help="max worst-modules to show in output (default 10)",
    )
    args = p.parse_args(argv)

    current = _walk_packages(args.packages)
    if not current:
        print("docstring-audit: GREEN -- no missing docstrings found")
        return 0

    if args.list_only:
        total = sum(len(v) for v in current.values())
        print(f"docstring-audit: {total} missing docstrings across {len(current)} files")
        for module in sorted(current):
            for m in current[module]:
                print(f"  {module}:{m['lineno']:>4}  [{m['kind']:>8}]  {m['symbol']}")
        return 0

    baseline = (
        json.loads(args.baseline.read_text(encoding="utf-8"))
        if args.baseline.exists()
        else {"per_module": {}, "samples": 0}
    )
    diagnostics, new_baseline = _evaluate(current, baseline)

    overall = max((d["level"] for d in diagnostics), key=_severity, default="GREEN")
    code = _severity(overall)

    total = sum(d["current"] for d in diagnostics)
    print(
        f"docstring-audit: {overall} -- {total} missing across {len(diagnostics)} files "
        f"(samples={baseline.get('samples', 0)} prior)",
    )
    # Sort by severity then by current count desc
    sorted_diag = sorted(
        diagnostics,
        key=lambda d: (-_severity(d["level"]), -d["current"]),
    )
    for d in sorted_diag[: args.max_show]:
        if d["level"] == "SEED":
            print(f"  [SEED  ] {d['module']}: {d['current']} missing (baseline-seed)")
            continue
        if d["level"] != "GREEN":
            base_str = str(d["baseline"]) if d["baseline"] is not None else "-"
            print(
                f"  [{d['level']:6}] {d['module']}: {d['current']} missing "
                f"vs baseline {base_str} (delta={d['delta']:+d})",
            )

    n_unchanged = sum(1 for d in diagnostics if d["level"] == "GREEN")
    n_seeded = sum(1 for d in diagnostics if d["level"] == "SEED")
    print(f"  ({n_unchanged} at-or-better, {n_seeded} new modules seeded)")

    if not args.no_update:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(new_baseline, indent=2) + "\n",
            encoding="utf-8",
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
