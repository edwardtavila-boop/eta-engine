"""One-command dev loop: ruff fix + ruff check + pytest + commit suggester.

Replaces the manual sequence::

    git add ...
    python -m ruff check --fix .
    python -m ruff check .
    python -m pytest -x -q
    git diff --cached --stat
    git commit -m "..."

with a single command::

    python scripts/_dev_loop.py            # full cycle, dry-run for commit
    python scripts/_dev_loop.py --commit   # actually create the commit
    python scripts/_dev_loop.py --quick    # skip pytest (ruff only)

Stages
------
1. **stage**: if --add-all, runs ``git add -A``. Otherwise reports what
   is currently staged and what is unstaged-but-tracked.
2. **ruff-fix**: ``python -m ruff check --fix`` on STAGED .py files.
   Auto-applies the safe fixes ruff knows about.
3. **ruff-check**: ``python -m ruff check`` on STAGED .py files.
   Anything left after fix needs manual attention.
4. **pytest**: full sweep with -x -q. Skipped if --quick.
5. **commit-suggest**: prints a suggested commit message based on the
   files staged. Heuristics:
       - If only scripts/_bump_roadmap_*.py: "chore(roadmap): bump to vX"
       - If only tests/: "test(<module>): ..."
       - If only docs/: "docs: ..."
       - Otherwise: "<verb>(<scope>): ..." with scope inferred
   Use --commit to actually run the commit (requires user input for
   the final message).

Exit codes
----------
0  all stages green, ready to commit
1  ruff issues remain after --fix (manual edits needed)
2  pytest failed
3  setup error (no python, no git, etc.)

Why this exists
---------------
Single-keystroke dev cycle reduces the cost of small commits, which
in turn keeps each commit reviewable. The operator was running these
commands manually 10-30x per day -- this is a 5-second-per-invocation
saver that compounds.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False, **kw)  # type: ignore[arg-type]


def _staged_py() -> list[str]:
    out = _run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    return [line.strip() for line in out.stdout.splitlines() if line.strip().endswith(".py")]


def _all_staged() -> list[str]:
    out = _run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _unstaged_tracked() -> list[str]:
    out = _run(["git", "diff", "--name-only", "--diff-filter=ACMR"])
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _ruff_fix(files: list[str]) -> tuple[bool, str]:
    if not files:
        return (True, "  no staged .py files -- skip")
    out = _run([sys.executable, "-m", "ruff", "check", "--fix", *files])
    return (out.returncode == 0, out.stdout + out.stderr)


def _ruff_check(files: list[str]) -> tuple[bool, str]:
    if not files:
        return (True, "  no staged .py files -- skip")
    out = _run([sys.executable, "-m", "ruff", "check", *files])
    return (out.returncode == 0, out.stdout + out.stderr)


def _pytest() -> tuple[bool, str]:
    out = _run([sys.executable, "-m", "pytest", "-x", "-q", "--no-header"])
    tail = "\n".join((out.stdout + out.stderr).splitlines()[-12:])
    return (out.returncode == 0, tail)


def _suggest_commit(staged: list[str]) -> str:
    if not staged:
        return "(nothing staged)"
    bumps = [f for f in staged if f.startswith("scripts/_bump_roadmap_v")]
    if bumps and len(bumps) == len(staged):
        ver = bumps[-1].split("_v")[-1].replace(".py", "").replace("_", ".")
        return f"chore(roadmap): bump to v{ver}"
    if all(f.startswith("docs/") for f in staged):
        return "docs: <describe documentation change>"
    if all(f.startswith("tests/") for f in staged):
        return "test: <describe test change>"
    if all(f.startswith("scripts/") and "monitor" in f or "drift" in f or "reconcile" in f for f in staged):
        return "feat(scripts): <new sentinel/automation>"
    if all(f.startswith("scripts/") for f in staged):
        return "chore(scripts): <describe script change>"
    if any(f.startswith("strategies/") for f in staged):
        return "feat(strategies): <describe strategy change>"
    if any(f.startswith("bots/") for f in staged):
        return "feat(bots): <describe bot change>"
    if any(f.startswith("core/") for f in staged):
        return "feat(core): <describe core change>"
    if any(f.startswith("brain/") for f in staged):
        return "feat(brain): <describe brain change>"
    if any(f.startswith("backtest/") for f in staged):
        return "feat(backtest): <describe backtest change>"
    return "<verb>(<scope>): <describe change>"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--add-all", action="store_true", help="git add -A before linting")
    p.add_argument("--commit", action="store_true", help="prompt for commit message and commit")
    p.add_argument("--quick", action="store_true", help="skip pytest")
    p.add_argument("--no-fix", action="store_true", help="skip ruff --fix")
    args = p.parse_args(argv)

    print("[dev-loop] stage 1: git status snapshot")
    if args.add_all:
        _run(["git", "add", "-A"])
        print("  git add -A done")
    staged = _all_staged()
    unstaged = _unstaged_tracked()
    print(f"  staged: {len(staged)}, unstaged-tracked: {len(unstaged)}")

    staged_py = _staged_py()

    if not args.no_fix:
        print("[dev-loop] stage 2: ruff --fix on staged .py")
        ok, out = _ruff_fix(staged_py)
        for line in out.splitlines()[-10:]:
            print(f"  {line}")
        if ok:
            # Re-stage any files ruff modified
            for f in staged_py:
                _run(["git", "add", f])

    print("[dev-loop] stage 3: ruff check on staged .py")
    ok, out = _ruff_check(staged_py)
    for line in out.splitlines()[-10:]:
        print(f"  {line}")
    if not ok:
        print("[dev-loop] FAIL -- ruff issues remain after --fix; fix manually")
        return 1

    if not args.quick:
        print("[dev-loop] stage 4: pytest -x -q")
        ok, tail = _pytest()
        for line in tail.splitlines():
            print(f"  {line}")
        if not ok:
            print("[dev-loop] FAIL -- pytest broke")
            return 2
    else:
        print("[dev-loop] stage 4: pytest skipped (--quick)")

    print("[dev-loop] stage 5: commit suggestion")
    suggested = _suggest_commit(staged)
    print(f"  suggested: {suggested}")

    if args.commit:
        print()
        print("Enter commit message (or press enter to use suggestion):")
        try:
            msg = input("> ").strip() or suggested
        except EOFError:
            print("[dev-loop] no input -- use --commit interactively")
            return 0
        # Append Claude co-author trailer if missing
        if "Co-Authored-By" not in msg:
            msg += "\n\nCo-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
        out = _run(["git", "commit", "-m", msg])
        print(out.stdout + out.stderr)

    print("[dev-loop] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
