"""Generate a pytest scaffold for a new source module.

Given a source path like ``strategies/adaptive_sizing.py``, this script
creates ``tests/test_strategies_adaptive_sizing.py`` populated with:

* Import smoke test (the module imports cleanly)
* AST-discovered class fixtures (one ``def test_<class>_smoke()`` per
  public class, attempting to instantiate with no args)
* AST-discovered function smoke tests (one per public function, with
  signature inspection to skip those that need non-default args)
* TODO markers for the operator to fill in real cases

The generated test file is conventional pytest -- no custom markers,
no async unless the source has ``async def``, no fixtures unless
operator wires them.

Usage
-----
    python scripts/_test_scaffold.py strategies/adaptive_sizing.py
    python scripts/_test_scaffold.py strategies/adaptive_sizing.py --force
    python scripts/_test_scaffold.py --batch  bots/  strategies/  brain/

If the target test file already exists, the script refuses to
overwrite unless --force is given.

Why this exists
---------------
Operator's tests are extremely thorough (45-49 tests per module is
typical). The first 5 tests are always the same shape: import smoke,
class smoke per public class, basic-input smoke per public function.
Generating those frees the operator to focus on the edge cases that
actually exercise behavior.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"

TEMPLATE_HEADER_BASE = '''"""Tests for ``{import_path}``.

Auto-scaffolded by scripts/_test_scaffold.py -- the import smoke and
the per-symbol smoke tests are boilerplate. Edit freely; the
operator-specific edge cases belong here.

WARNING: per-symbol smoke tests instantiate the class. If a class
registers global state on construction (loggers, signal handlers,
journal writers, kill switches, etc.) the smoke test will POLLUTE
other tests in the suite. Convert those to per-test fixtures or
delete the smoke test.
"""
from __future__ import annotations

import importlib
{pytest_import}

def test_import_smoke() -> None:
    """Module imports without raising."""
    importlib.import_module("{import_path}")
'''

CLASS_TEMPLATE = '''

def test_{snake_name}_smoke() -> None:
    """``{cls_name}`` instantiates with no args (or skips if it requires args)."""
    from {import_path} import {cls_name}
    try:
        obj = {cls_name}()  # type: ignore[call-arg]
    except Exception as e:  # noqa: BLE001 -- pydantic/dataclass/attrs all raise differently
        pytest.skip(f"{cls_name} requires args: {{type(e).__name__}}: {{e}}")
    else:
        assert obj is not None
        # TODO: real assertions about default state
'''

FUNC_TEMPLATE = '''

def test_{snake_name}_smoke() -> None:
    """``{fn_name}`` is callable (signature requires manual fill-in)."""
    from {import_path} import {fn_name}
    assert callable({fn_name})
    # TODO: invoke with realistic inputs and assert on output
'''

ASYNC_FUNC_TEMPLATE = '''

@pytest.mark.asyncio
async def test_{snake_name}_smoke() -> None:
    """``{fn_name}`` is an async callable (signature requires manual fill-in)."""
    import inspect

    from {import_path} import {fn_name}
    assert inspect.iscoroutinefunction({fn_name})
    # TODO: await with realistic inputs and assert on output
'''


def _src_to_test_path(src: Path) -> Path:
    """``strategies/adaptive_sizing.py`` -> ``tests/test_strategies_adaptive_sizing.py``."""
    rel = src.relative_to(ROOT)
    parts = list(rel.parts)
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]  # strip .py
    name = "_".join(parts)
    return TESTS_DIR / f"test_{name}.py"


def _import_path(src: Path) -> str:
    """``strategies/adaptive_sizing.py`` -> ``eta_engine.strategies.adaptive_sizing``."""
    rel = src.relative_to(ROOT)
    parts = list(rel.parts)
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return "eta_engine." + ".".join(parts)


def _to_snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _scan_symbols(src: Path) -> tuple[list[str], list[str], list[str]]:
    """Return (classes, sync_funcs, async_funcs) -- public only."""
    try:
        tree = ast.parse(src.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return ([], [], [])
    classes: list[str] = []
    sync_funcs: list[str] = []
    async_funcs: list[str] = []
    for node in tree.body:
        name = getattr(node, "name", "")
        if not name or name.startswith("_"):
            continue
        if isinstance(node, ast.ClassDef):
            classes.append(name)
        elif isinstance(node, ast.AsyncFunctionDef):
            async_funcs.append(name)
        elif isinstance(node, ast.FunctionDef):
            sync_funcs.append(name)
    return (classes, sync_funcs, async_funcs)


def _scaffold_one(src: Path, *, force: bool) -> tuple[bool, str]:
    if not src.exists():
        return (False, f"source not found: {src}")
    if src.suffix != ".py":
        return (False, f"not a .py file: {src}")
    if src.name.startswith("_") or src.name.startswith("test_"):
        return (False, f"skip private/test file: {src}")
    test_path = _src_to_test_path(src)
    if test_path.exists() and not force:
        return (False, f"test file already exists: {test_path} (use --force)")
    import_path = _import_path(src)
    classes, sync_funcs, async_funcs = _scan_symbols(src)
    # pytest is only needed when classes (skip) or async funcs (mark.asyncio) exist
    pytest_needed = bool(classes) or bool(async_funcs)
    pytest_import = "\nimport pytest\n" if pytest_needed else ""
    body = TEMPLATE_HEADER_BASE.format(
        import_path=import_path,
        pytest_import=pytest_import,
    )
    for cls in classes:
        body += CLASS_TEMPLATE.format(
            snake_name=_to_snake(cls),
            cls_name=cls,
            import_path=import_path,
        )
    for fn in sync_funcs:
        body += FUNC_TEMPLATE.format(
            snake_name=_to_snake(fn),
            fn_name=fn,
            import_path=import_path,
        )
    for fn in async_funcs:
        body += ASYNC_FUNC_TEMPLATE.format(
            snake_name=_to_snake(fn),
            fn_name=fn,
            import_path=import_path,
        )
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(body, encoding="utf-8")
    n_total = 1 + len(classes) + len(sync_funcs) + len(async_funcs)
    return (True, f"wrote {test_path} ({n_total} tests scaffolded)")


def _expand_batch(targets: list[str]) -> list[Path]:
    out: list[Path] = []
    for t in targets:
        p = ROOT / t
        if p.is_dir():
            out.extend(p.rglob("*.py"))
        elif p.is_file():
            out.append(p)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("paths", nargs="+", help="source file(s) or dir(s)")
    p.add_argument("--force", action="store_true", help="overwrite existing test files")
    p.add_argument(
        "--batch",
        action="store_true",
        help="treat positional args as dirs and recurse into them",
    )
    args = p.parse_args(argv)

    targets: list[Path]
    if args.batch:
        targets = _expand_batch(args.paths)
    else:
        targets = [Path(t) if Path(t).is_absolute() else ROOT / t for t in args.paths]

    n_ok = 0
    n_skip = 0
    for src in targets:
        ok, msg = _scaffold_one(src, force=args.force)
        prefix = "scaffold:" if ok else "skip:    "
        print(f"{prefix} {msg}")
        if ok:
            n_ok += 1
        else:
            n_skip += 1
    print(f"--- summary: {n_ok} scaffolded, {n_skip} skipped ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
