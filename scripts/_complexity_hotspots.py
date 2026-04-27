"""Rank functions by McCabe cyclomatic complexity. Top N hotspots.

Walks every .py file's AST and computes McCabe per function. High
complexity = high refactor risk = high test priority.

Usage
-----
    python scripts/_complexity_hotspots.py            # top 20
    python scripts/_complexity_hotspots.py --top 50
    python scripts/_complexity_hotspots.py --threshold 15  # only show >=N
    python scripts/_complexity_hotspots.py --json

McCabe complexity counts decision points:
    +1 base
    +1 each: if/elif, for, while, except, with, and/or, ternary, comprehension
    +1 each case in match-statement
    +1 each assert

Score interpretation (Knuth/Mccabe):
     1- 5  simple
     6-10  moderate
    11-20  complex (refactor candidate)
     21+   high risk (URGENT refactor)

Exit codes
----------
0  GREEN  -- no function above --threshold
1  YELLOW -- 1..--max-yellow above threshold
2  RED    -- >--max-yellow above threshold
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".cache", "node_modules", ".ruff_cache"}


@dataclass
class FuncScore:
    path: str
    line: int
    name: str
    score: int
    n_lines: int


class _ComplexityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.score = 1  # base

    def visit_If(self, node: ast.If) -> None:  # noqa: N802
        self.score += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:  # noqa: N802
        self.score += 1
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:  # noqa: N802
        self.score += 1
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:  # noqa: N802
        self.score += 1
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        self.score += 1
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:  # noqa: N802
        self.score += 1
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802
        self.score += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:  # noqa: N802
        self.score += len(node.values) - 1
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:  # noqa: N802
        self.score += 1
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:  # noqa: N802
        self.score += 1
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:  # noqa: N802
        self.score += 1 + len(node.ifs)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:  # noqa: N802
        self.score += len(node.cases)
        self.generic_visit(node)


def _score_func(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    v = _ComplexityVisitor()
    v.generic_visit(node)
    return v.score


def _scan_file(path: Path) -> list[FuncScore]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    rel = str(path.relative_to(ROOT)).replace("\\", "/")
    out: list[FuncScore] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n_lines = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
            out.append(
                FuncScore(
                    path=rel,
                    line=node.lineno,
                    name=node.name,
                    score=_score_func(node),
                    n_lines=n_lines,
                )
            )
    return out


def _scan_all() -> list[FuncScore]:
    out: list[FuncScore] = []
    for path in ROOT.rglob("*.py"):
        if any(p in SKIP_DIRS for p in path.parts):
            continue
        out.extend(_scan_file(path))
    out.sort(key=lambda f: -f.score)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--top", type=int, default=20, help="show top N hotspots")
    p.add_argument("--threshold", type=int, default=15, help="complexity threshold for YELLOW/RED (default 15)")
    p.add_argument("--max-yellow", type=int, default=10, help="more than this many over threshold -> RED")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    funcs = _scan_all()
    if args.json:
        print(json.dumps([asdict(f) for f in funcs[: args.top]], indent=2))
        return 0

    over = [f for f in funcs if f.score >= args.threshold]
    n = len(over)

    print(
        f"complexity-hotspots: top {min(args.top, len(funcs))} of {len(funcs)} functions "
        f"(threshold>={args.threshold} -> {n} above)",
    )
    print()
    print(f"  {'score':>5}  {'lines':>5}  {'function':<40}  location")
    print(f"  {'-' * 5}  {'-' * 5}  {'-' * 40}  {'-' * 40}")
    for f in funcs[: args.top]:
        marker = "*" if f.score >= args.threshold else " "
        loc = f"{f.path}:{f.line}"
        print(f"  {f.score:>5}  {f.n_lines:>5}  {marker} {f.name[:38]:<40}  {loc}")

    if n == 0:
        print(f"\ncomplexity-hotspots: GREEN -- no function above {args.threshold}")
        return 0
    level = "RED" if n > args.max_yellow else "YELLOW"
    print(f"\ncomplexity-hotspots: {level} -- {n} function(s) above {args.threshold}")
    return 1 if level == "YELLOW" else 2


if __name__ == "__main__":
    sys.exit(main())
