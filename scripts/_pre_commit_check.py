"""Pre-commit hygiene gate for eta_engine.

Runs ruff + pytest before any commit and refuses to let the commit
proceed if either fails. Exit codes:

  0 -> all checks passed, commit may proceed
  1 -> ruff failed
  2 -> pytest failed
  3 -> setup error (e.g. cannot find pytest or ruff)

Usage
-----
Direct:

    python scripts/_pre_commit_check.py

As a git pre-commit hook (one-time install):

    python scripts/_pre_commit_check.py --install-hook

The hook runs the full pytest sweep and ruff on production code only
(strategies/ + scripts/). Test-file annotation noise is intentionally
not enforced -- production cleanliness is what blocks the commit.

Why this exists
---------------
The v0.1.32-v0.1.45 work sat unstaged on disk for hours, invisible to
the cloud automation fleet that was cloning the repo. A pre-commit
gate combined with the nightly stale-work watchdog ensures that the
GitHub snapshot stays close to local truth.

Design constraints
------------------
* Pure stdlib + subprocess -- nothing to install
* Fast path: ruff first (sub-second), then pytest (~12s)
* --quick flag skips pytest for tiny doc-only commits
* --no-pytest also skips, but loudly warns
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK_BODY = """#!/bin/sh
# eta_engine pre-commit hygiene gate (auto-installed)
exec python scripts/_pre_commit_check.py
"""


def _run(cmd: list[str], *, cwd: Path) -> int:
    """Run a subprocess, stream output to stderr, return exit code."""
    print(f"  $ {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, cwd=cwd, check=False)
    return proc.returncode


def _staged_python_files(*, root: Path) -> list[str]:
    """Return staged .py files (relative paths). Empty list -> nothing to lint."""
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return []
    return [line for line in out.stdout.splitlines() if line.endswith(".py") and (root / line).exists()]


def _ruff_check(*, root: Path) -> int:
    """Ruff over staged .py files only.

    Linting the whole tree would surface pre-existing issues in legacy
    scripts that the operator hasn't been maintaining -- and a gate that
    cries wolf gets disabled. Only what the user is about to commit gets
    checked.
    """
    files = _staged_python_files(root=root)
    if not files:
        print("[pre-commit] no staged .py files; skipping ruff", file=sys.stderr)
        return 0
    rc = _run(["python", "-m", "ruff", "check", *files], cwd=root)
    if rc != 0:
        print(
            f"[pre-commit] FAIL: ruff found issues in {len(files)} staged file(s)",
            file=sys.stderr,
        )
    return rc


def _pytest_check(*, root: Path) -> int:
    """Full pytest sweep, fail fast."""
    rc = _run(
        ["python", "-m", "pytest", "-x", "-q", "--no-header"],
        cwd=root,
    )
    if rc != 0:
        print(
            "[pre-commit] FAIL: pytest reports broken tests",
            file=sys.stderr,
        )
    return rc


def _install_hook(*, root: Path) -> int:
    """Write .git/hooks/pre-commit pointing at this script."""
    hooks_dir = root / ".git" / "hooks"
    if not hooks_dir.exists():
        print(
            f"[pre-commit] cannot install: {hooks_dir} does not exist (is this a git repo?)",
            file=sys.stderr,
        )
        return 3
    hook_path = hooks_dir / "pre-commit"
    hook_path.write_text(HOOK_BODY, encoding="utf-8")
    # Best-effort chmod +x (no-op on Windows)
    with contextlib.suppress(OSError):
        hook_path.chmod(0o755)
    print(f"[pre-commit] installed -> {hook_path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--install-hook",
        action="store_true",
        help="install this script as .git/hooks/pre-commit",
    )
    p.add_argument(
        "--quick",
        action="store_true",
        help="skip pytest (only run ruff) -- use for doc-only commits",
    )
    p.add_argument(
        "--no-pytest",
        action="store_true",
        help="skip pytest with a loud warning",
    )
    args = p.parse_args(argv)

    if args.install_hook:
        return _install_hook(root=ROOT)

    print("[pre-commit] running ruff...", file=sys.stderr)
    rc = _ruff_check(root=ROOT)
    if rc != 0:
        return 1

    skip_pytest = args.quick or args.no_pytest
    if args.quick:
        print(
            "[pre-commit] --quick -> skipping pytest (ruff passed)",
            file=sys.stderr,
        )
    elif args.no_pytest:
        print(
            "[pre-commit] --no-pytest -> WARNING: skipping pytest, you are committing untested code",
            file=sys.stderr,
        )
    else:
        print("[pre-commit] running pytest...", file=sys.stderr)
        rc = _pytest_check(root=ROOT)
        if rc != 0:
            return 2

    # Advisory audits (do NOT gate the commit -- if a future bare
    # deferral or unknown alert event surfaces, the operator sees the
    # message inline at commit time but the commit still proceeds).
    # Promoting any of these to gating is a follow-up decision; today
    # they're advisory because the cost of a false positive blocking a
    # routine commit is higher than the risk of a real positive going
    # unnoticed (the audits also run on demand via
    # ``python scripts/_audit_*.py``).
    #
    # Audits run even in --quick / --no-pytest mode because they're
    # cheap (sub-second) and catch the kind of drift that doesn't
    # surface in the unit-test suite.
    _ = skip_pytest  # used only as documentation for the comment above
    _advisory_audits(root=ROOT)

    print("[pre-commit] OK -- commit may proceed", file=sys.stderr)
    return 0


def _advisory_audits(*, root: Path) -> None:
    """Run the three audit scripts in advisory mode and surface results.

    Failures here do NOT block the commit. They print to stderr so the
    operator sees them inline; that's the only intervention. To
    promote any audit to a hard gate, change the call site to inspect
    the return code and ``return 4`` (or similar) on non-zero.
    """
    audits = [
        ("alert-events", "scripts/_audit_alert_events.py", []),
        ("roadmap-vs-code", "scripts/_audit_roadmap_vs_code.py", []),
        ("deferral-criteria", "scripts/_audit_deferral_criteria.py", []),
    ]
    for label, script, extra_args in audits:
        path = root / script
        if not path.exists():
            print(
                f"[pre-commit] advisory: {label} -- {script} missing, skipping",
                file=sys.stderr,
            )
            continue
        print(f"[pre-commit] advisory: {label}...", file=sys.stderr)
        result = subprocess.run(
            ["python", str(path), *extra_args],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        # Print the audit's stdout summary so the operator sees the
        # report without having to run the script separately.
        if result.returncode != 0:
            # Audit reports an issue. Surface stdout (the summary)
            # but DO NOT propagate the non-zero exit -- advisory mode.
            tail = result.stdout.rstrip().splitlines()[-15:]
            print(
                f"[pre-commit] advisory: {label} reports issues (rc={result.returncode}, NOT blocking):",
                file=sys.stderr,
            )
            for line in tail:
                print(f"[pre-commit]   {line}", file=sys.stderr)
        else:
            # Show the last summary line so silent passes still confirm.
            lines = [ln for ln in result.stdout.rstrip().splitlines() if ln.strip()]
            if lines:
                print(f"[pre-commit]   {lines[-1]}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
