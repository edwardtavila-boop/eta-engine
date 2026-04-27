"""One-shot WIP triage: bucket untracked files into commit-readiness groups.

Reads `git status --short` and bins each untracked file as one of:
- src .py with companion test (Bucket A: commit-ready)
- src .py without companion test (Bucket B: needs tests)
- test .py (Bucket C: ride-along with their src)
- one-shot bump scripts (Bucket D: usually committable as-is)
- docs/ artifacts (Bucket E: report outputs, low-priority)
- broken .py (Bucket F: doesn't compile)
"""

from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent


def py_compiles(p: pathlib.Path) -> bool:
    try:
        compile(p.read_text(encoding="utf-8"), str(p), "exec")
        return True
    except (SyntaxError, UnicodeDecodeError, FileNotFoundError):
        return False


def posix(p: pathlib.Path) -> str:
    return str(p).replace("\\", "/")


def has_companion_test(p: pathlib.Path) -> bool:
    if "test" in p.name or "tests/" in posix(p):
        return True
    name = p.stem
    candidates = [
        ROOT / "tests" / f"test_{name}.py",
        ROOT / "tests" / f"test_{p.parent.name}_{name}.py",
        ROOT / "tests" / p.parent.name / f"test_{name}.py",
    ]
    return any(c.exists() for c in candidates)


def main() -> None:
    out = subprocess.run(
        ["git", "status", "--short"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    untracked: list[pathlib.Path] = []
    for line in out.stdout.splitlines():
        if not line.startswith("??"):
            continue
        path = line[3:].strip()
        if ".cache" in path:
            continue
        untracked.append(ROOT / path)

    expanded: list[pathlib.Path] = []
    for p in untracked:
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    expanded.append(f)
        elif p.is_file():
            expanded.append(p)

    ready_py: list[pathlib.Path] = []
    untested_py: list[pathlib.Path] = []
    test_files: list[pathlib.Path] = []
    broken_py: list[pathlib.Path] = []
    artifacts: list[pathlib.Path] = []
    scripts_bump: list[pathlib.Path] = []
    other: list[pathlib.Path] = []

    for p in expanded:
        s = posix(p.relative_to(ROOT))
        if s.startswith("docs/") or "_backups/" in s:
            artifacts.append(p)
        elif p.suffix == ".py":
            if "scripts/_bump_roadmap_v" in s:
                scripts_bump.append(p)
            elif s.startswith("tests/"):
                (test_files if py_compiles(p) else broken_py).append(p)
            elif not py_compiles(p):
                broken_py.append(p)
            elif not has_companion_test(p):
                untested_py.append(p)
            else:
                ready_py.append(p)
        else:
            other.append(p)

    print(f"TRIAGE: {len(expanded)} untracked files")
    print(f"  src .py with companion test:    {len(ready_py)}")
    print(f"  src .py WITHOUT companion test: {len(untested_py)}")
    print(f"  test .py:                       {len(test_files)}")
    print(f"  broken .py:                     {len(broken_py)}")
    print(f"  one-shot bump scripts:          {len(scripts_bump)}")
    print(f"  docs/ artifacts:                {len(artifacts)}")
    print(f"  other:                          {len(other)}")
    print()
    print("=== Bucket A: SRC .PY WITH TEST (commit-ready) ===")
    for p in sorted(ready_py):
        print(f"  {posix(p.relative_to(ROOT))}")
    print()
    print("=== Bucket B: SRC .PY WITHOUT COMPANION TEST ===")
    for p in sorted(untested_py):
        print(f"  {posix(p.relative_to(ROOT))}")
    print()
    print(f"=== Bucket C: TEST FILES ({len(test_files)}) ===")
    for p in sorted(test_files):
        print(f"  {posix(p.relative_to(ROOT))}")
    print()
    print(f"=== Bucket D: ONE-SHOT BUMP SCRIPTS ({len(scripts_bump)}) ===")
    for p in sorted(scripts_bump):
        print(f"  {posix(p.relative_to(ROOT))}")
    print()
    if broken_py:
        print("=== Bucket F (BROKEN) ===")
        for p in broken_py:
            print(f"  {posix(p.relative_to(ROOT))}")


if __name__ == "__main__":
    main()
