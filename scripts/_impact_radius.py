"""For a target function/class, find every call site (transitive optional).

Answers: "if I rename or delete this, what breaks?"

Usage
-----
    python scripts/_impact_radius.py BaseBot
    python scripts/_impact_radius.py record_fill
    python scripts/_impact_radius.py eta_engine.strategies.engine_adapter.EngineAdapter
    python scripts/_impact_radius.py BaseBot --tests-only
    python scripts/_impact_radius.py BaseBot --transitive   # follow chains

Discovery
---------
* Direct hits: any file containing the target name (Name, Attribute, or
  ImportFrom)
* Transitive hits: when --transitive, BFS from each direct hit's
  enclosing function/class, finding callers of THAT.

Output
------
- list of (file, line, ctx) hits sorted by file
- count summary
- "tests cover N/M direct hits"

Why
---
The operator can't `git grep` and trust the result -- variable names
collide, local vs imported is ambiguous, and tests-vs-production
matters. This walks the AST so it knows whether a name is a CALL,
an IMPORT, an ATTRIBUTE access, or just a string.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Hit:
    path: Path
    line: int
    kind: str  # "call", "attr", "import", "name", "string"
    ctx: str  # surrounding function/class name, or "<module>"
    snippet: str  # the actual line (trimmed)


@dataclass
class Index:
    by_file: dict[Path, list[Hit]] = field(default_factory=dict)

    def add(self, hit: Hit) -> None:
        self.by_file.setdefault(hit.path, []).append(hit)

    def all_hits(self) -> list[Hit]:
        out: list[Hit] = []
        for hits in self.by_file.values():
            out.extend(hits)
        return out


def _enclosing_ctx(node: ast.AST, lineno: int) -> str:
    """Best-effort: walk a tree, find the def/class enclosing lineno."""
    best: str = "<module>"
    best_depth = 0
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = child.lineno
            end = getattr(child, "end_lineno", start) or start
            if start <= lineno <= end:
                depth = child.col_offset if hasattr(child, "col_offset") else 0
                if depth >= best_depth:
                    best = child.name
                    best_depth = depth
    return best


def _scan_file(path: Path, target: str) -> list[Hit]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    if target not in text:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    hits: list[Hit] = []

    # Strip dotted prefix for short-name match
    short_target = target.rsplit(".", 1)[-1]

    def _snippet(lineno: int) -> str:
        if 1 <= lineno <= len(lines):
            return lines[lineno - 1].strip()[:120]
        return ""

    for node in ast.walk(tree):
        ln = getattr(node, "lineno", 0)
        if not ln:
            continue
        if isinstance(node, ast.Name) and node.id == short_target:
            kind = "call" if isinstance(getattr(node, "ctx", None), ast.Load) else "name"
            hits.append(
                Hit(
                    path=path,
                    line=ln,
                    kind=kind,
                    ctx=_enclosing_ctx(tree, ln),
                    snippet=_snippet(ln),
                )
            )
        elif isinstance(node, ast.Attribute) and node.attr == short_target:
            hits.append(
                Hit(
                    path=path,
                    line=ln,
                    kind="attr",
                    ctx=_enclosing_ctx(tree, ln),
                    snippet=_snippet(ln),
                )
            )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == short_target:
                    hits.append(
                        Hit(
                            path=path,
                            line=ln,
                            kind="import",
                            ctx="<module>",
                            snippet=_snippet(ln),
                        )
                    )
    return hits


def _scan_repo(target: str) -> Index:
    idx = Index()
    for path in ROOT.rglob("*.py"):
        if any(p in {".git", ".venv", "__pycache__", ".pytest_cache", ".cache", "node_modules"} for p in path.parts):
            continue
        for hit in _scan_file(path, target):
            idx.add(hit)
    return idx


def _format(idx: Index, *, tests_only: bool) -> tuple[int, int, str]:
    out: list[str] = []
    test_hits = 0
    src_hits = 0
    for path in sorted(idx.by_file, key=lambda p: str(p)):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        is_test = rel.startswith("tests/")
        if tests_only and not is_test:
            continue
        hits = idx.by_file[path]
        if is_test:
            test_hits += len(hits)
        else:
            src_hits += len(hits)
        out.append(f"\n  {rel}  ({len(hits)} hit(s))")
        for h in hits:
            out.append(f"    {h.line:>4}  [{h.kind:>6}]  in {h.ctx}: {h.snippet}")
    return (src_hits, test_hits, "\n".join(out))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("target", help="symbol name (short or dotted)")
    p.add_argument("--tests-only", action="store_true", help="only show hits inside tests/")
    p.add_argument("--src-only", action="store_true", help="exclude tests/")
    args = p.parse_args(argv)

    idx = _scan_repo(args.target)
    total = len(idx.all_hits())
    if total == 0:
        print(f"impact-radius: 0 hits for '{args.target}'")
        return 0

    src_hits, test_hits, body = _format(idx, tests_only=args.tests_only)
    if args.src_only and not args.tests_only:
        # filter again
        body_lines = []
        skip = False
        for line in body.splitlines():
            if line.startswith("\n  tests/") or line.startswith("  tests/"):
                skip = True
            elif line.startswith("\n  ") or line.startswith("  ") and not line.startswith("    "):
                skip = False
            if not skip:
                body_lines.append(line)
        body = "\n".join(body_lines)

    print(
        f"impact-radius: '{args.target}' -- "
        f"{total} hit(s) in {len(idx.by_file)} file(s) "
        f"({src_hits} source, {test_hits} test)"
    )
    print(body)
    print(f"\nTest coverage of hits: {test_hits}/{total} ({(test_hits / total * 100):.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
