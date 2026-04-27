"""Find .py files that no other .py file imports.

An orphan module is a candidate for deletion -- it's compiled,
linted, and tested for nothing. Keeping orphans around degrades
codebase navigation and inflates surface area.

Detection
---------
For each candidate ``<package>/<sub>/<name>.py``:
* Compute the dotted path ``eta_engine.<package>.<sub>.<name>``
* Search every other .py in the repo for either:
    ``from eta_engine.<package>.<sub>.<name> ...``
    ``import eta_engine.<package>.<sub>.<name>``
    ``from eta_engine.<package>.<sub> import <name>``
* If ZERO matches outside the file itself, it's an orphan

Skips
-----
* __init__.py (re-export only)
* scripts/* (entry points, intentionally not imported)
* tests/* (intentionally not imported)
* files starting with `_` (private)
* files matching --extra-skip patterns

Exit codes
----------
0  GREEN  -- no orphans
1  YELLOW -- 1..--max-yellow orphans
2  RED    -- > --max-yellow orphans
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
SKIP_DIRS = {"scripts", "tests", "__pycache__", ".git", ".venv", ".pytest_cache", ".cache", ".ruff_cache"}


def _dotted(rel: Path) -> str:
    parts = list(rel.parts)
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return "eta_engine." + ".".join(parts)


def _is_imported_anywhere(target: Path, dotted: str, parent_dotted: str, name: str) -> bool:
    needles = (
        f"from {dotted} ",
        f"from {dotted}\n",
        f"from {dotted}\\",
        f"import {dotted}",
        f"from {parent_dotted} import {name}",
        f"from {parent_dotted} import (\n",  # multi-line, refined check below
    )
    for path in ROOT.rglob("*.py"):
        if any(p in SKIP_DIRS for p in path.parts):
            continue
        if path == target:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(n in text for n in needles[:5]):
            return True
        # Check multi-line `from X import (\n    name,\n    ...)`
        if f"from {parent_dotted} import" in text and name in text:
            # Quick verification: name appears within 200 chars of the import
            idx = text.find(f"from {parent_dotted} import")
            window = text[idx : idx + 400]
            if name in window:
                return True
    return False


def _find_orphans(packages: list[str]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for pkg in packages:
        pkg_dir = ROOT / pkg
        if not pkg_dir.exists():
            continue
        for path in pkg_dir.rglob("*.py"):
            if path.name == "__init__.py":
                continue
            if path.name.startswith("_"):
                continue
            rel = path.relative_to(ROOT)
            dotted = _dotted(rel)
            parent_dotted = ".".join(dotted.split(".")[:-1])
            name = dotted.split(".")[-1]
            if _is_imported_anywhere(path, dotted, parent_dotted, name):
                continue
            try:
                loc = sum(
                    1
                    for ln in path.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                )
            except OSError:
                loc = 0
            out.append((str(rel).replace("\\", "/"), loc))
    out.sort(key=lambda x: -x[1])
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--packages", nargs="+", default=DEFAULT_PACKAGES)
    p.add_argument("--max-yellow", type=int, default=5)
    args = p.parse_args(argv)

    orphans = _find_orphans(args.packages)
    n = len(orphans)
    if n == 0:
        print("orphan-files: GREEN -- no orphan modules detected")
        return 0
    level = "RED" if n > args.max_yellow else "YELLOW"
    print(f"orphan-files: {level} -- {n} orphan module(s)")
    for rel, loc in orphans:
        print(f"  {loc:>4} loc  {rel}")
    print(
        "\nIf intentional (entry point or experimental), add the path to your skip list. Otherwise consider deletion.",
    )
    return 1 if level == "YELLOW" else 2


if __name__ == "__main__":
    sys.exit(main())
