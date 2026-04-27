"""Build the cross-package import graph and flag cycles.

For every .py file under the configured packages, walk the AST and
extract its `import eta_engine.X` and `from eta_engine.X import ...`
edges. Aggregate to package-level (e.g. `bots -> strategies`) and:

* Print the adjacency list (which package depends on which)
* Run a DFS to detect cycles
* If cycles exist, print each one and exit RED

Output
------
    import-graph: 9 packages
       bots         -> brain, core, obs, strategies, venues
       brain        -> core
       backtest     -> brain, core, strategies
       core         ->
       funnel       -> obs
       obs          -> core
       strategies   -> brain, core, obs, venues
       staking      -> core
       venues       -> core

    Cycles: 0  GREEN

Exit codes
----------
0  GREEN  -- no cycles
1  YELLOW -- 1..--max-yellow cycles
2  RED    -- > --max-yellow cycles

Usage
-----
    python scripts/_import_graph.py
    python scripts/_import_graph.py --json
    python scripts/_import_graph.py --packages bots strategies core

Why
---
A cycle between packages means you can't reason about layering. Catch
the first one before it becomes seven and the package boundary is mush.
"""

from __future__ import annotations

import argparse
import ast
import json
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


def _edges_from_file(path: Path, packages: set[str]) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, OSError):
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mod = node.module
            if mod.startswith("eta_engine."):
                top = mod.split(".")[1]
                if top in packages:
                    out.add(top)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name.startswith("eta_engine."):
                    top = name.split(".")[1]
                    if top in packages:
                        out.add(top)
    return out


def _build(packages: list[str]) -> dict[str, set[str]]:
    pkg_set = set(packages)
    graph: dict[str, set[str]] = {p: set() for p in packages}
    for pkg in packages:
        pkg_dir = ROOT / pkg
        if not pkg_dir.exists():
            continue
        for path in pkg_dir.rglob("*.py"):
            if any(p in SKIP_DIRS for p in path.parts):
                continue
            for dep in _edges_from_file(path, pkg_set):
                if dep != pkg:
                    graph[pkg].add(dep)
    return graph


# DFS coloring states for cycle detection
_WHITE, _GRAY, _BLACK = 0, 1, 2


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return each elementary cycle once (rotated to start at min node)."""
    cycles: set[tuple[str, ...]] = set()
    color: dict[str, int] = {n: _WHITE for n in graph}
    stack: list[str] = []

    def dfs(node: str) -> None:
        color[node] = _GRAY
        stack.append(node)
        for dep in sorted(graph.get(node, ())):
            if color[dep] == _GRAY:
                # cycle: stack[stack.index(dep):]
                cyc = tuple(stack[stack.index(dep) :])
                # normalize: rotate so smallest element is first
                lo = min(range(len(cyc)), key=lambda i: cyc[i])
                cyc_norm = cyc[lo:] + cyc[:lo]
                cycles.add(cyc_norm)
            elif color[dep] == _WHITE:
                dfs(dep)
        color[node] = _BLACK
        stack.pop()

    for n in sorted(graph):
        if color[n] == _WHITE:
            dfs(n)
    return [list(c) for c in sorted(cycles)]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--packages", nargs="+", default=DEFAULT_PACKAGES)
    p.add_argument("--max-yellow", type=int, default=0, help="more than N cycles -> RED (default 0: any cycle is RED)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    graph = _build(args.packages)
    cycles = _find_cycles(graph)

    if args.json:
        print(
            json.dumps(
                {
                    "graph": {k: sorted(v) for k, v in graph.items()},
                    "cycles": cycles,
                },
                indent=2,
            )
        )
        if not cycles:
            return 0
        return 2 if len(cycles) > args.max_yellow else 1

    print(f"import-graph: {len(graph)} packages")
    width = max(len(p) for p in graph) if graph else 0
    for pkg in sorted(graph):
        deps = ", ".join(sorted(graph[pkg])) if graph[pkg] else "(none)"
        print(f"   {pkg.ljust(width)}  -> {deps}")

    print()
    if not cycles:
        print("Cycles: 0  GREEN")
        return 0
    level = "RED" if len(cycles) > args.max_yellow else "YELLOW"
    print(f"Cycles: {len(cycles)}  {level}")
    for cyc in cycles:
        print(f"  -> {' -> '.join(cyc)} -> {cyc[0]}")
    return 1 if level == "YELLOW" else 2


if __name__ == "__main__":
    sys.exit(main())
